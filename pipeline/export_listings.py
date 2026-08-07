"""Scrape (optional) and export listings to site/data/listings.json."""

from __future__ import annotations

import argparse
import json
import logging
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
    items = []
    for row in rows:
        item = {k: _json_safe(row.get(k)) for k in EXPORT_KEYS if k in row}
        items.append(item)

    payload = {
        "exported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "count": len(items),
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
