"""SQLite persistence for listings, notes, categories, and scrape logs."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from pipeline.config import CATEGORIES, DATA_DIR, DB_PATH


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    ensure_data_dir()
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT,
    url TEXT NOT NULL UNIQUE,
    title TEXT,
    description TEXT,
    price REAL,
    price_cold REAL,
    rooms REAL,
    size_sqm REAL,
    floor TEXT,
    address TEXT,
    district TEXT,
    city TEXT DEFAULT 'Augsburg',
    lat REAL,
    lon REAL,
    distance_uni_km REAL,
    distance_center_km REAL,
    transit_uni_min INTEGER,
    transit_uni_transfers INTEGER,
    transit_uni_summary TEXT,
    available_from TEXT,
    furnished INTEGER DEFAULT 0,
    balcony INTEGER DEFAULT 0,
    sofa INTEGER DEFAULT 0,
    own_bathroom INTEGER DEFAULT 1,
    parking INTEGER DEFAULT 0,
    deposit REAL,
    max_persons INTEGER,
    rental_duration TEXT,
    term_type TEXT,
    tenancy_type TEXT,
    image_urls TEXT DEFAULT '[]',
    amenities TEXT DEFAULT '[]',
    status TEXT DEFAULT 'active',
    match_score REAL DEFAULT 0,
    score_breakdown TEXT DEFAULT '{}',
    geo_precision TEXT DEFAULT 'exact',
    hidden INTEGER DEFAULT 0,
    category TEXT DEFAULT 'unreviewed',
    notes TEXT DEFAULT '',
    is_new INTEGER DEFAULT 1,
    is_manual INTEGER DEFAULT 0,
    first_seen TEXT,
    last_seen TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS scrape_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    listings_found INTEGER DEFAULT 0,
    listings_new INTEGER DEFAULT 0,
    message TEXT
);

CREATE INDEX IF NOT EXISTS idx_listings_source ON listings(source);
CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status);
CREATE INDEX IF NOT EXISTS idx_listings_category ON listings(category);
CREATE INDEX IF NOT EXISTS idx_listings_score ON listings(match_score DESC);
CREATE INDEX IF NOT EXISTS idx_listings_price ON listings(price);
"""


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Migrations for databases created before new columns existed
        cols = {r[1] for r in conn.execute("PRAGMA table_info(listings)").fetchall()}
        if "geo_precision" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN geo_precision TEXT DEFAULT 'exact'")
        if "hidden" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN hidden INTEGER DEFAULT 0")
        if "transit_uni_min" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN transit_uni_min INTEGER")
        if "transit_uni_transfers" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN transit_uni_transfers INTEGER")
        if "transit_uni_summary" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN transit_uni_summary TEXT")
        if "term_type" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN term_type TEXT")
        if "tenancy_type" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN tenancy_type TEXT")


def _serialize_list(value: Any) -> str:
    if value is None:
        return "[]"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _serialize_dict(value: Any) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def row_to_dict(row: sqlite3.Row | None) -> Optional[dict]:
    if row is None:
        return None
    d = dict(row)
    for key in ("image_urls", "amenities"):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key] or "[]")
            except json.JSONDecodeError:
                d[key] = []
    if "score_breakdown" in d and isinstance(d["score_breakdown"], str):
        try:
            d["score_breakdown"] = json.loads(d["score_breakdown"] or "{}")
        except json.JSONDecodeError:
            d["score_breakdown"] = {}
    for key in ("furnished", "balcony", "sofa", "own_bathroom", "parking", "is_new", "is_manual", "hidden"):
        if key in d and d[key] is not None:
            d[key] = bool(d[key])
    return d


UPSERT_FIELDS = [
    "source",
    "external_id",
    "url",
    "title",
    "description",
    "price",
    "price_cold",
    "rooms",
    "size_sqm",
    "floor",
    "address",
    "district",
    "city",
    "lat",
    "lon",
    "distance_uni_km",
    "distance_center_km",
    "transit_uni_min",
    "transit_uni_transfers",
    "transit_uni_summary",
    "available_from",
    "furnished",
    "balcony",
    "sofa",
    "own_bathroom",
    "parking",
    "deposit",
    "max_persons",
    "rental_duration",
    "term_type",
    "tenancy_type",
    "image_urls",
    "amenities",
    "status",
    "match_score",
    "score_breakdown",
    "geo_precision",
    "is_new",
    "first_seen",
    "last_seen",
    "updated_at",
]


def upsert_listing(data: dict) -> tuple[int, bool]:
    """Insert or update a listing by URL. Returns (id, is_new).

    User fields (category, notes, is_manual) are preserved on update.
    """
    now = _utc_now()
    url = data["url"]
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, category, notes, is_manual, image_urls, description FROM listings WHERE url = ?",
            (url,),
        ).fetchone()

        payload = dict(data)
        payload["image_urls"] = _serialize_list(payload.get("image_urls"))
        payload["amenities"] = _serialize_list(payload.get("amenities"))
        payload["score_breakdown"] = _serialize_dict(payload.get("score_breakdown"))
        for bool_key in ("furnished", "balcony", "sofa", "own_bathroom", "parking", "is_new", "is_manual"):
            if bool_key in payload and payload[bool_key] is not None:
                payload[bool_key] = 1 if payload[bool_key] else 0

        if existing:
            # Preserve user annotations
            listing_id = existing["id"]

            # Don't downgrade enriched data: when a rescrape's detail fetch
            # fails (rate limit etc.), the item carries only the 0-1 card
            # thumbnail and a short card blob — keep the stored gallery /
            # full description in that case.
            try:
                old_imgs = json.loads(existing["image_urls"] or "[]")
                new_imgs = json.loads(payload.get("image_urls") or "[]")
                if len(new_imgs) < 2 and len(old_imgs) > len(new_imgs):
                    payload["image_urls"] = existing["image_urls"]
            except (TypeError, ValueError):
                pass
            old_desc = existing["description"] or ""
            new_desc = payload.get("description") or ""
            if new_desc and old_desc and len(new_desc) < len(old_desc) * 0.6:
                payload["description"] = old_desc
            payload["last_seen"] = now
            payload["updated_at"] = now
            payload["is_new"] = 0
            # Don't overwrite user category/notes unless explicitly provided for manual
            set_clause = ", ".join(
                f"{f} = ?"
                for f in UPSERT_FIELDS
                if f not in ("url", "first_seen", "is_new") and f in payload
            )
            values = [
                payload[f]
                for f in UPSERT_FIELDS
                if f not in ("url", "first_seen", "is_new") and f in payload
            ]
            values.append(listing_id)
            conn.execute(
                f"UPDATE listings SET {set_clause}, is_new = 0 WHERE id = ?",
                values,
            )
            return listing_id, False

        payload.setdefault("first_seen", now)
        payload.setdefault("last_seen", now)
        payload.setdefault("updated_at", now)
        payload.setdefault("is_new", 1)
        payload.setdefault("status", "active")
        payload.setdefault("category", "unreviewed")
        payload.setdefault("notes", "")
        payload.setdefault("is_manual", 0)
        payload.setdefault("city", "Augsburg")

        cols = [f for f in UPSERT_FIELDS if f in payload] + ["category", "notes", "is_manual"]
        # dedupe cols
        seen = set()
        unique_cols = []
        for c in cols:
            if c not in seen:
                seen.add(c)
                unique_cols.append(c)
        placeholders = ", ".join("?" for _ in unique_cols)
        col_names = ", ".join(unique_cols)
        values = [payload.get(c) for c in unique_cols]
        cur = conn.execute(
            f"INSERT INTO listings ({col_names}) VALUES ({placeholders})",
            values,
        )
        return cur.lastrowid, True


def mark_missing_as_gone(source: str, seen_urls: set[str]) -> int:
    """Mark active listings from source not seen in this scrape as rented/gone."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, url FROM listings WHERE source = ? AND status = 'active' AND is_manual = 0",
            (source,),
        ).fetchall()
        gone = 0
        now = _utc_now()
        for row in rows:
            if row["url"] not in seen_urls:
                conn.execute(
                    "UPDATE listings SET status = 'gone', updated_at = ?, is_new = 0 WHERE id = ?",
                    (now, row["id"]),
                )
                gone += 1
        return gone


def get_listing(listing_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
        return row_to_dict(row)


def list_listings(
    *,
    status: Optional[str] = "active",
    category: Optional[str] = None,
    shortlist: Optional[str] = None,
    term_type: Optional[str] = None,
    tenancy_type: Optional[str] = None,
    source: Optional[str] = None,
    price_max: Optional[float] = None,
    price_min: Optional[float] = None,
    rooms_min: Optional[float] = None,
    size_min: Optional[float] = None,
    photos_min: Optional[int] = None,
    distance_max: Optional[float] = None,
    transit_max: Optional[int] = None,
    available_before: Optional[str] = None,
    q: Optional[str] = None,
    sort: str = "score",
    include_gone: bool = False,
    hidden: Optional[bool] = False,
) -> list[dict]:
    clauses: list[str] = []
    params: list[Any] = []

    # hidden=False -> only visible, hidden=True -> only hidden, hidden=None -> all
    if hidden is False:
        clauses.append("(hidden IS NULL OR hidden = 0)")
    elif hidden is True:
        clauses.append("hidden = 1")

    if status and not include_gone:
        clauses.append("status = ?")
        params.append(status)
    elif not include_gone:
        clauses.append("status != 'gone'")

    mode = (shortlist or "all").lower()
    if mode == "only":
        clauses.append("category = 'shortlist'")
    elif mode == "hide":
        clauses.append("(category IS NULL OR category != 'shortlist')")
    elif category:
        clauses.append("category = ?")
        params.append(category)

    if term_type in ("short", "long"):
        clauses.append("term_type = ?")
        params.append(term_type)
    if tenancy_type in ("owner", "sublet"):
        clauses.append("tenancy_type = ?")
        params.append(tenancy_type)

    if source:
        clauses.append("source = ?")
        params.append(source)
    if price_max is not None:
        clauses.append("(price IS NULL OR price <= ?)")
        params.append(price_max)
    if price_min is not None:
        clauses.append("(price IS NULL OR price >= ?)")
        params.append(price_min)
    if rooms_min is not None:
        clauses.append("(rooms IS NULL OR rooms >= ?)")
        params.append(rooms_min)
    if size_min is not None:
        clauses.append("(size_sqm IS NULL OR size_sqm >= ?)")
        params.append(size_min)
    if distance_max is not None:
        clauses.append("(distance_uni_km IS NULL OR distance_uni_km <= ?)")
        params.append(distance_max)
    if transit_max is not None:
        # Keep listings still waiting for a route; -1 means "no route found"
        clauses.append(
            "(transit_uni_min IS NULL OR transit_uni_min < 0 OR transit_uni_min <= ?)"
        )
        params.append(transit_max)
    if available_before:
        clauses.append("(available_from IS NULL OR available_from <= ? OR available_from = 'sofort')")
        params.append(available_before)
    if q:
        clauses.append("(title LIKE ? OR description LIKE ? OR address LIKE ? OR district LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like, like])

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    order = {
        "score": "match_score DESC, price ASC",
        "price_asc": "price ASC NULLS LAST",
        "price_desc": "price DESC NULLS LAST",
        "newest": "first_seen DESC",
        "distance": "distance_uni_km ASC NULLS LAST",
        "transit": "transit_uni_min ASC NULLS LAST",
        "size": "size_sqm DESC NULLS LAST",
    }.get(sort, "match_score DESC, price ASC")

    # SQLite doesn't support NULLS LAST in older versions; emulate
    order = (
        order.replace(" ASC NULLS LAST", " IS NULL, price ASC")
        .replace(" DESC NULLS LAST", " IS NULL, price DESC")
    )
    if sort == "price_asc":
        order = "price IS NULL, price ASC"
    elif sort == "price_desc":
        order = "price IS NULL, price DESC"
    elif sort == "distance":
        order = "distance_uni_km IS NULL, distance_uni_km ASC"
    elif sort == "transit":
        order = "transit_uni_min IS NULL, transit_uni_min ASC"
    elif sort == "size":
        order = "size_sqm IS NULL, size_sqm DESC"

    sql = f"SELECT * FROM listings{where} ORDER BY {order}"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        items = [row_to_dict(r) for r in rows]

    if photos_min is not None and photos_min > 0:
        def _photo_count(item: dict) -> int:
            urls = item.get("image_urls") or []
            n = 0
            for u in urls:
                if not isinstance(u, str):
                    continue
                if u.startswith(("http://", "https://")) and not u.startswith("data:"):
                    n += 1
            return n

        items = [i for i in items if _photo_count(i) >= photos_min]
    return items


def update_listing_fields(listing_id: int, fields: dict) -> Optional[dict]:
    allowed = {
        "category",
        "notes",
        "status",
        "title",
        "description",
        "price",
        "rooms",
        "size_sqm",
        "address",
        "district",
        "available_from",
        "furnished",
        "balcony",
        "sofa",
        "own_bathroom",
        "parking",
        "lat",
        "lon",
        "distance_uni_km",
        "distance_center_km",
        "transit_uni_min",
        "transit_uni_transfers",
        "transit_uni_summary",
        "match_score",
        "score_breakdown",
        "geo_precision",
        "hidden",
        "term_type",
        "tenancy_type",
        "image_urls",
        "amenities",
        "is_new",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_listing(listing_id)

    if "category" in updates and updates["category"] not in CATEGORIES:
        raise ValueError(f"Invalid category: {updates['category']}")

    for key in ("furnished", "balcony", "sofa", "own_bathroom", "parking", "is_new", "hidden"):
        if key in updates and updates[key] is not None:
            updates[key] = 1 if updates[key] else 0
    if "image_urls" in updates:
        updates["image_urls"] = _serialize_list(updates["image_urls"])
    if "amenities" in updates:
        updates["amenities"] = _serialize_list(updates["amenities"])
    if "score_breakdown" in updates:
        updates["score_breakdown"] = _serialize_dict(updates["score_breakdown"])

    updates["updated_at"] = _utc_now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [listing_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE listings SET {set_clause} WHERE id = ?", values)
    return get_listing(listing_id)


def mark_all_seen() -> int:
    with get_conn() as conn:
        cur = conn.execute("UPDATE listings SET is_new = 0 WHERE is_new = 1")
        return cur.rowcount


def delete_listing(listing_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM listings WHERE id = ?", (listing_id,))
        return cur.rowcount > 0


def log_scrape(
    source: str,
    status: str,
    listings_found: int = 0,
    listings_new: int = 0,
    message: str = "",
    started_at: Optional[str] = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO scrape_log
            (source, started_at, finished_at, status, listings_found, listings_new, message)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source,
                started_at or _utc_now(),
                _utc_now(),
                status,
                listings_found,
                listings_new,
                message,
            ),
        )


def get_scrape_logs(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM scrape_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_stats() -> dict:
    with get_conn() as conn:
        visible = "status = 'active' AND (hidden IS NULL OR hidden = 0)"
        total = conn.execute(f"SELECT COUNT(*) AS c FROM listings WHERE {visible}").fetchone()["c"]
        new = conn.execute(f"SELECT COUNT(*) AS c FROM listings WHERE {visible} AND is_new = 1").fetchone()["c"]
        hidden_count = conn.execute(
            "SELECT COUNT(*) AS c FROM listings WHERE hidden = 1"
        ).fetchone()["c"]
        by_cat = {
            r["category"]: r["c"]
            for r in conn.execute(
                f"SELECT category, COUNT(*) AS c FROM listings WHERE {visible} GROUP BY category"
            )
        }
        by_source = {
            r["source"]: r["c"]
            for r in conn.execute(
                f"SELECT source, COUNT(*) AS c FROM listings WHERE {visible} GROUP BY source"
            )
        }
        last_scrape = conn.execute(
            "SELECT * FROM scrape_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "total_active": total,
            "new_count": new,
            "hidden_count": hidden_count,
            "by_category": by_cat,
            "by_source": by_source,
            "last_scrape": dict(last_scrape) if last_scrape else None,
        }


def listings_needing_geocode(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM listings
            WHERE lat IS NULL OR lon IS NULL
               OR geo_precision IS NULL
               OR geo_precision = ''
               OR (geo_precision = 'city' AND (address IS NOT NULL OR district IS NOT NULL))
            ORDER BY CASE WHEN lat IS NULL THEN 0 ELSE 1 END, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def listings_missing_coords(limit: int = 500) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM listings
            WHERE lat IS NULL OR lon IS NULL
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def listings_needing_transit(limit: int = 120) -> list[dict]:
    """Active listings with coordinates but no metro/tram time yet."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM listings
            WHERE lat IS NOT NULL AND lon IS NOT NULL
              AND transit_uni_min IS NULL
              AND status != 'gone'
            ORDER BY
              CASE category WHEN 'shortlist' THEN 0 WHEN 'contacted' THEN 1 ELSE 2 END,
              match_score DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def backfill_term_tenancy(limit: int = 500) -> int:
    """Recompute short/long + owner/sublet labels from stored text."""
    from pipeline.scoring import detect_term_tenancy

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, title, description, amenities, rental_duration, term_type, tenancy_type
            FROM listings
            WHERE status != 'gone'
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    updated = 0
    for row in rows:
        item = dict(row)
        amenities = item.get("amenities") or "[]"
        if isinstance(amenities, str):
            try:
                amenities = json.loads(amenities)
            except json.JSONDecodeError:
                amenities = []
        text = " ".join(
            filter(
                None,
                [
                    item.get("title") or "",
                    item.get("description") or "",
                    " ".join(amenities) if isinstance(amenities, list) else "",
                ],
            )
        )
        labels = detect_term_tenancy(text, item.get("rental_duration"))
        term = labels.get("term_type")
        tenancy = labels.get("tenancy_type")
        if term == item.get("term_type") and tenancy == item.get("tenancy_type"):
            continue
        if not term and not tenancy and not item.get("term_type") and not item.get("tenancy_type"):
            continue
        fields = {}
        if term:
            fields["term_type"] = term
        if tenancy:
            fields["tenancy_type"] = tenancy
        if fields:
            update_listing_fields(item["id"], fields)
            updated += 1
    return updated
