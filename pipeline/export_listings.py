"""Scrape (optional) and export listings to site/data/listings.json."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is on sys.path when run as a script
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline import db
from pipeline.config import (
    CITY_CENTER,
    CONFIG_JSON,
    LISTINGS_JSON,
    MOVE_IN_TARGET,
    PRICE_HARD_MAX,
    PRICE_IDEAL_MAX,
    PRICE_IDEAL_MIN,
    SITE_DATA_DIR,
    UNI_AUGSBURG,
)
from pipeline.scrapers.runner import (
    ensure_studentenwerk_resource,
    fill_transit_times,
    geocode_pending,
    run_all_scrapers,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("export")

# Fields written into the public JSON snapshot
EXPORT_KEYS = [
    "id",
    "source",
    "url",
    "title",
    "description",
    "price",
    "rooms",
    "size_sqm",
    "address",
    "district",
    "lat",
    "lon",
    "geo_precision",
    "distance_uni_km",
    "distance_center_km",
    "transit_uni_min",
    "transit_uni_transfers",
    "transit_uni_summary",
    "image_urls",
    "status",
    "match_score",
    "score_breakdown",
    "furnished",
    "balcony",
    "sofa",
    "parking",
    "own_bathroom",
    "term_type",
    "tenancy_type",
    "available_from",
    "deposit",
    "amenities",
    "first_seen",
    "last_seen",
    "is_new",
]


def _json_safe(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


# Prefer primary portals over aggregators when collapsing cross-source copies.
_SOURCE_RANK = {
    "hc24": 0,
    "studentenwerk": 1,
    "wg_gesucht": 2,
    "kleinanzeigen": 3,
    "immosurf": 4,
    "immowelt": 5,
    "immonet": 6,
    "wohnungsboerse": 7,
    "immobilienscout24": 8,
}

_GENERIC_TITLES = {
    "wohnung augsburg",
    "zimmer augsburg",
    "apartment augsburg",
    "wg zimmer augsburg",
    "1 zimmer wohnung augsburg",
    "2 zimmer wohnung augsburg",
}


def _norm_title(title: str | None) -> str:
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r"[\"'`]", "", t)
    t = re.sub(r"[^a-z0-9äöüß\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _title_word_count(norm: str) -> int:
    return sum(1 for w in norm.split() if len(w) > 2)


def _num_close(a, b, tol: float) -> bool | None:
    """True/False if both known; None if either missing."""
    if a is None or b is None:
        return None
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return None


def _listings_are_duplicates(a: dict, b: dict) -> bool:
    """Soft match: same normalized title + price, with size/rooms guards."""
    ta = _norm_title(a.get("title"))
    tb = _norm_title(b.get("title"))
    if not ta or ta != tb:
        return False
    pa, pb = a.get("price"), b.get("price")
    if pa is None or pb is None:
        return False
    try:
        if abs(float(pa) - float(pb)) > 1.0:
            return False
    except (TypeError, ValueError):
        return False

    rooms_ok = _num_close(a.get("rooms"), b.get("rooms"), tol=0.05)
    size_ok = _num_close(a.get("size_sqm"), b.get("size_sqm"), tol=2.5)

    generic = ta in _GENERIC_TITLES or _title_word_count(ta) < 5
    if generic:
        # Vague titles need rooms + size both present and matching.
        return rooms_ok is True and size_ok is True

    # Specific titles: reject only on clear conflicts.
    if rooms_ok is False or size_ok is False:
        return False
    return True


def _richness(row: dict) -> tuple:
    images = row.get("image_urls") or []
    n_img = len(images) if isinstance(images, list) else 0
    desc = row.get("description") or ""
    score = row.get("match_score")
    try:
        score_v = float(score) if score is not None else -1.0
    except (TypeError, ValueError):
        score_v = -1.0
    has_geo = 1 if row.get("lat") is not None and row.get("lon") is not None else 0
    has_transit = 1 if row.get("transit_uni_min") is not None else 0
    src = _SOURCE_RANK.get(str(row.get("source") or ""), 50)
    # Higher is better except source rank (lower = preferred).
    return (n_img, len(desc), score_v, has_geo, has_transit, -src)


def dedupe_listings(rows: list[dict]) -> list[dict]:
    """Collapse cross-source / near-identical copies; keep richest row per cluster."""
    n = len(rows)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(n):
        for j in range(i + 1, n):
            if _listings_are_duplicates(rows[i], rows[j]):
                union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    kept: list[dict] = []
    removed = 0
    for idxs in clusters.values():
        if len(idxs) == 1:
            kept.append(rows[idxs[0]])
            continue
        best_i = max(idxs, key=lambda i: _richness(rows[i]))
        winner = dict(rows[best_i])
        alt_urls = []
        alt_sources = []
        for i in idxs:
            if i == best_i:
                continue
            removed += 1
            u = rows[i].get("url")
            s = rows[i].get("source")
            if u and u != winner.get("url"):
                alt_urls.append(u)
            if s and s != winner.get("source") and s not in alt_sources:
                alt_sources.append(s)
        if alt_urls:
            winner["duplicate_urls"] = alt_urls
        if alt_sources:
            winner["duplicate_sources"] = alt_sources
        kept.append(winner)

    kept.sort(
        key=lambda r: (
            float(r["match_score"]) if r.get("match_score") is not None else -1.0
        ),
        reverse=True,
    )
    if removed:
        logger.info("Deduped %s duplicate listing(s); %s unique remain", removed, len(kept))
    return kept


def export_config() -> dict:
    return {
        "university": UNI_AUGSBURG,
        "city_center": CITY_CENTER,
        "price_ideal_min": PRICE_IDEAL_MIN,
        "price_ideal_max": PRICE_IDEAL_MAX,
        "price_hard_max": PRICE_HARD_MAX,
        "move_in_target": MOVE_IN_TARGET,
        "exported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


def export_listings_json() -> int:
    SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()
    db.backfill_term_tenancy(limit=2000)

    rows = db.list_listings(
        status=None,
        include_gone=True,
        hidden=None,
        sort="score",
    )
    raw_items = []
    for row in rows:
        item = {k: _json_safe(row.get(k)) for k in EXPORT_KEYS if k in row}
        raw_items.append(item)

    before = len(raw_items)
    items = [_json_safe(it) for it in dedupe_listings(raw_items)]

    payload = {
        "exported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "count": len(items),
        "before_dedupe": before,
        "listings": items,
    }
    LISTINGS_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    CONFIG_JSON.write_text(
        json.dumps(export_config(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Wrote %s listings → %s", len(items), LISTINGS_JSON)
    return len(items)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scrape and export listings for GitHub Pages")
    parser.add_argument(
        "--skip-scrape",
        action="store_true",
        help="Only export existing SQLite data (no network scrape)",
    )
    parser.add_argument(
        "--no-geocode",
        action="store_true",
        help="Skip geocode/transit fill after scrape",
    )
    args = parser.parse_args(argv)

    db.init_db()
    ensure_studentenwerk_resource()

    if not args.skip_scrape:
        logger.info("Running scrapers…")
        results = run_all_scrapers(do_geocode=not args.no_geocode)
        logger.info("Scrape results: %s", results)
        if not args.no_geocode:
            n_geo = geocode_pending(limit=120)
            n_transit = fill_transit_times(limit=80)
            logger.info("Geocoded %s · transit filled %s", n_geo, n_transit)
    else:
        logger.info("Skipping scrape (--skip-scrape)")

    n = export_listings_json()
    logger.info("Done. %s listings exported.", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
