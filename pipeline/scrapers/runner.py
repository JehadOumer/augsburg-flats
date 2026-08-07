"""Run all scrapers and optional geocoding pass."""

from __future__ import annotations

import logging
from typing import Optional

from pipeline import db
from pipeline.config import STUDENTENWERK_INFO
from pipeline.scoring import prepare_listing
from pipeline.scrapers.fallbacks import ensure_is24_fallback_card
from pipeline.scrapers.hc24 import HC24Scraper
from pipeline.scrapers.immoscout import ImmoScout24Scraper
from pipeline.scrapers.immosurf import ImmosurfScraper
from pipeline.scrapers.immowelt import ImmonetScraper, ImmoweltScraper
from pipeline.scrapers.kleinanzeigen import KleinanzeigenScraper
from pipeline.scrapers.wg_gesucht import WGGesuchtScraper
from pipeline.scrapers.wohnungsboerse import WohnungsboerseScraper

logger = logging.getLogger(__name__)

SCRAPER_CLASSES = [
    HC24Scraper,
    ImmosurfScraper,
    KleinanzeigenScraper,
    WGGesuchtScraper,
    ImmoweltScraper,
    ImmonetScraper,
    WohnungsboerseScraper,
    ImmoScout24Scraper,
]


def ensure_studentenwerk_resource() -> None:
    """Insert a static Studentenwerk info card if missing."""
    url = STUDENTENWERK_INFO["url"]
    existing = None
    with db.get_conn() as conn:
        row = conn.execute("SELECT id FROM listings WHERE url = ?", (url,)).fetchone()
        existing = row
    if existing:
        return
    listing = prepare_listing(
        {
            "source": "studentenwerk",
            "url": url,
            "title": STUDENTENWERK_INFO["title"],
            "description": STUDENTENWERK_INFO["notes"],
            "price": None,
            "address": "Augsburg",
            "city": "Augsburg",
            "district": "Student housing",
            "lat": 48.3345,
            "lon": 10.8974,
            "furnished": True,
            "own_bathroom": True,
            "image_urls": [],
            "amenities": ["student housing", "waiting list"],
            "status": "active",
            "is_manual": 0,
        },
        do_geocode=False,
    )
    listing["match_score"] = 50.0
    listing["score_breakdown"] = {
        "note": "Official Studentenwerk resource — apply via their portal",
        "total": 50.0,
    }
    db.upsert_listing(listing)


def geocode_pending(limit: int = 80) -> int:
    """Ensure listings have map coordinates (exact, district, or city fallback)."""
    from pipeline.geo import resolve_coordinates

    # Prefer listings still missing coords entirely
    pending = db.listings_missing_coords(limit=limit)
    if len(pending) < limit:
        more = db.listings_needing_geocode(limit=limit - len(pending))
        seen = {p["id"] for p in pending}
        pending.extend(m for m in more if m["id"] not in seen)

    updated = 0
    for item in pending:
        # Clear lat to force full resolution when missing; keep existing if present
        working = dict(item)
        if working.get("lat") is not None and working.get("lon") is not None:
            # Already on map — only fill precision label if empty
            if working.get("geo_precision"):
                continue
        lat, lon, precision = resolve_coordinates(
            working,
            do_nominatim=working.get("lat") is None,
        )
        from pipeline.scoring import apply_distances, score_listing

        working["lat"] = lat
        working["lon"] = lon
        working["geo_precision"] = precision
        working = apply_distances(working)
        score, breakdown = score_listing(working)
        db.update_listing_fields(
            item["id"],
            {
                "lat": lat,
                "lon": lon,
                "geo_precision": precision,
                "distance_uni_km": working.get("distance_uni_km"),
                "distance_center_km": working.get("distance_center_km"),
                "match_score": score,
                "score_breakdown": breakdown,
            },
        )
        updated += 1
    return updated


def fill_transit_times(limit: int = 100) -> int:
    """Compute tram/bus minutes to the university for listings that need it."""
    from pipeline.transit import transit_to_uni

    pending = db.listings_needing_transit(limit=limit)
    updated = 0
    for item in pending:
        route = transit_to_uni(item.get("lat"), item.get("lon"))
        if not route:
            # Mark as unknown with 0 so we don't retry forever on dead spots;
            # use a sentinel: store nothing and skip by writing a huge value? 
            # Better: leave NULL and rely on cache negatives — but listings_needing
            # would retry. Write minutes=-1 for "no route".
            db.update_listing_fields(
                item["id"],
                {
                    "transit_uni_min": -1,
                    "transit_uni_transfers": None,
                    "transit_uni_summary": None,
                },
            )
            continue
        db.update_listing_fields(
            item["id"],
            {
                "transit_uni_min": route["minutes"],
                "transit_uni_transfers": route["transfers"],
                "transit_uni_summary": route["summary"],
            },
        )
        updated += 1
    return updated


def ensure_all_map_pins() -> int:
    """Force every listing without coordinates onto the map."""
    from pipeline.geo import resolve_coordinates
    from pipeline.scoring import apply_distances, score_listing

    missing = db.listings_missing_coords(limit=1000)
    updated = 0
    for item in missing:
        lat, lon, precision = resolve_coordinates(item, do_nominatim=True)
        working = dict(item)
        working["lat"] = lat
        working["lon"] = lon
        working["geo_precision"] = precision
        working = apply_distances(working)
        score, breakdown = score_listing(working)
        db.update_listing_fields(
            item["id"],
            {
                "lat": lat,
                "lon": lon,
                "geo_precision": precision,
                "distance_uni_km": working.get("distance_uni_km"),
                "distance_center_km": working.get("distance_center_km"),
                "match_score": score,
                "score_breakdown": breakdown,
            },
        )
        updated += 1
    return updated


def run_all_scrapers(
    *,
    sources: Optional[list[str]] = None,
    do_geocode: bool = True,
) -> list[dict]:
    ensure_studentenwerk_resource()
    results = []
    for cls in SCRAPER_CLASSES:
        if sources and cls.source not in sources:
            continue
        logger.info("Running scraper: %s", cls.source)
        scraper = cls()
        # Geocode only for first few sources to avoid Nominatim hammering;
        # a follow-up pass handles the rest.
        result = scraper.run(do_geocode=do_geocode and cls.source == "hc24", mark_gone=True)
        results.append(result)
        logger.info("Finished %s: %s", cls.source, result)
        if cls.source == "immobilienscout24":
            try:
                ensure_is24_fallback_card()
            except Exception as exc:  # noqa: BLE001
                logger.debug("IS24 fallback card: %s", exc)
    try:
        n = ensure_all_map_pins()
        logger.info("Ensured map pins for %s listings", n)
        n2 = geocode_pending(limit=40)
        logger.info("Geocode/refine pass updated %s listings", n2)
        n3 = fill_transit_times(limit=80)
        logger.info("Transit fill updated %s listings", n3)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Geocode/transit pass failed: %s", exc)
    return results


def run_one(source: str, *, do_geocode: bool = True) -> dict:
    ensure_studentenwerk_resource()
    for cls in SCRAPER_CLASSES:
        if cls.source == source:
            result = cls().run(do_geocode=do_geocode, mark_gone=True)
            if source == "immobilienscout24":
                ensure_is24_fallback_card()
            try:
                fill_transit_times(limit=40)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Transit fill after %s: %s", source, exc)
            return result
    raise ValueError(f"Unknown source: {source}")
