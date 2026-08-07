"""Ensure visible fallback cards when blocked sources yield nothing."""

from __future__ import annotations

from pipeline import db
from pipeline.config import FALLBACK_SEARCH_LINKS
from pipeline.scoring import prepare_listing


def ensure_is24_fallback_card() -> None:
    """If ImmoScout24 scraping is empty/blocked, keep a searchable link card."""
    url = FALLBACK_SEARCH_LINKS.get("immobilienscout24")
    if not url:
        return
    with db.get_conn() as conn:
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM listings WHERE source = ? AND status = 'active' AND url NOT LIKE ?",
            ("immobilienscout24", "%Suche%"),
        ).fetchone()["c"]
        existing = conn.execute(
            "SELECT id FROM listings WHERE url = ?",
            (url,),
        ).fetchone()
    if active > 0:
        # Real listings present — remove the placeholder search card if any
        if existing:
            db.delete_listing(existing["id"])
        return
    listing = prepare_listing(
        {
            "source": "immobilienscout24",
            "url": url,
            "title": "ImmoScout24 – Augsburg search (open manually)",
            "description": (
                "ImmobilienScout24 blocks automated scraping (HTTP 401). "
                "Open this link in your browser to browse Augsburg apartments "
                "up to €900, then use + Add listing to save favorites here."
            ),
            "price": None,
            "address": "Augsburg",
            "city": "Augsburg",
            "image_urls": [],
            "amenities": ["manual search", "is24"],
            "status": "active",
            "is_manual": 0,
        },
        do_geocode=False,
    )
    listing["match_score"] = 45.0
    listing["score_breakdown"] = {"note": "Manual IS24 search — bot access blocked", "total": 45.0}
    lid, _ = db.upsert_listing(listing)
    db.update_listing_fields(lid, {"category": "unreviewed"})
