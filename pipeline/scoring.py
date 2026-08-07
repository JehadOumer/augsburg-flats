"""Match scoring and geocoding helpers."""

from __future__ import annotations

import math
import re
import time
from typing import Optional
from urllib.parse import quote

import httpx

from pipeline.config import (
    CITY_CENTER,
    NICE_TO_HAVE_KEYWORDS,
    PRICE_HARD_MAX,
    PRICE_IDEAL_MAX,
    PRICE_IDEAL_MIN,
    SHARED_KEYWORDS,
    UNI_AUGSBURG,
    USER_AGENT,
)

_GEOCODE_CACHE: dict[str, tuple[Optional[float], Optional[float]]] = {}
_LAST_GEOCODE_TS = 0.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def detect_features(text: str) -> dict[str, bool]:
    t = (text or "").lower()
    return {
        "balcony": any(k in t for k in ("balkon", "balcony", "terrasse", "terrace")),
        "sofa": any(k in t for k in ("sofa", "couch", "sitzecke")),
        "furnished": any(
            k in t
            for k in ("möbliert", "moebliert", "furnished", "eingerichtet", "vollmöbliert")
        ),
        "parking": any(k in t for k in ("parkplatz", "stellplatz", "parking", "garage")),
        "own_bathroom": not any(
            k in t for k in ("gemeinschaftsbad", "shared bathroom", "bad zur mitbenutzung")
        ),
        "is_shared": any(k in t for k in SHARED_KEYWORDS),
    }


def detect_term_tenancy(
    text: str, rental_duration: str | None = None
) -> dict[str, Optional[str]]:
    """Infer short/long term and owner/sublet from listing text."""
    blob = f"{text or ''} {rental_duration or ''}".lower()

    term: Optional[str] = None
    long_kw = (
        "unbefristet",
        "langfristig",
        "long-term",
        "long term",
        "longterm",
        "dauerhaft",
        "mindestens 12",
        "min. 12",
        "mind. 12",
        "min. 1 jahr",
        "mind. 1 jahr",
        "mindestens ein jahr",
        "unbefristeter mietvertrag",
    )
    short_kw = (
        "kurzzeit",
        "kurzfrist",
        "short-term",
        "short term",
        "shortterm",
        "temporär",
        "temporaer",
        "temporary",
        "wochenweise",
        "monatsweise",
        "befristet",
        "übergangsmiete",
        "uebergangsmiete",
        "für wenige monate",
        "max. 6 monate",
        "max 6 monate",
        "bis zu 6 monate",
        "1-3 monate",
        "2-3 monate",
        "3-6 monate",
        "few months",
    )
    if any(k in blob for k in long_kw):
        term = "long"
    elif any(k in blob for k in short_kw) or "zwischenmiete" in blob:
        term = "short"

    tenancy: Optional[str] = None
    sublet_kw = (
        "untervermiet",
        "untermiete",
        "sublet",
        "subletting",
        "zwischenmiete",
        "untervermieter",
        "weitervermietet",
        "von mieter",
    )
    owner_kw = (
        "von privat",
        "direkt vom eigentümer",
        "direkt vom eigentuemer",
        "eigentümer vermietet",
        "vom eigentümer",
        "vom eigentuemer",
        "privat vermietet",
        "direktvermietung",
        "first-hand",
        "first hand",
        "ohne zwischenmieter",
        "direkt vom vermieter",
    )
    if any(k in blob for k in sublet_kw):
        tenancy = "sublet"
    elif any(k in blob for k in owner_kw):
        tenancy = "owner"

    return {"term_type": term, "tenancy_type": tenancy}


def keyword_bonus(text: str) -> tuple[float, list[str]]:
    t = (text or "").lower()
    hits = [k for k in NICE_TO_HAVE_KEYWORDS if k in t]
    # unique-ish: group balcony variants etc.
    unique = set()
    for h in hits:
        if h in ("balkon", "balcony"):
            unique.add("balcony")
        elif h in ("sofa", "couch"):
            unique.add("sofa")
        elif h in ("möbliert", "moebliert", "furnished"):
            unique.add("furnished")
        elif h in ("wlan", "wifi", "internet"):
            unique.add("internet")
        elif h in ("parkplatz", "parking", "stellplatz"):
            unique.add("parking")
        else:
            unique.add(h)
    return min(25.0, len(unique) * 5.0), sorted(unique)


def score_price(price: Optional[float]) -> tuple[float, str]:
    if price is None:
        return 5.0, "price unknown"
    if price > PRICE_HARD_MAX:
        over = price - PRICE_HARD_MAX
        # steep penalty above hard max
        return max(-40.0, -20.0 - over / 10.0), f"over budget ({price:.0f}€ > {PRICE_HARD_MAX}€)"
    if PRICE_IDEAL_MIN <= price <= PRICE_IDEAL_MAX:
        return 40.0, f"ideal price ({price:.0f}€)"
    if price < PRICE_IDEAL_MIN:
        # still good if cheaper, but very low prices can be cold-rent-only or rooms
        if price < 300:
            return 15.0, f"suspiciously low ({price:.0f}€) — check if cold rent / room"
        return 35.0 + min(5.0, (PRICE_IDEAL_MIN - price) / 40.0), f"below ideal ({price:.0f}€)"
    # between ideal max and hard max
    ratio = (PRICE_HARD_MAX - price) / (PRICE_HARD_MAX - PRICE_IDEAL_MAX)
    return 15.0 + 20.0 * ratio, f"acceptable ({price:.0f}€)"


def score_distance(distance_uni_km: Optional[float], distance_center_km: Optional[float]) -> tuple[float, str]:
    if distance_uni_km is None and distance_center_km is None:
        return 5.0, "location unknown"
    # Prefer closer to uni; city center also fine
    best = None
    label = ""
    if distance_uni_km is not None:
        best = distance_uni_km
        label = f"{distance_uni_km:.1f} km to uni"
    if distance_center_km is not None:
        if best is None or distance_center_km < best:
            # city center is acceptable; use slightly softer scoring
            pass
        # score based on min of uni and center (with uni preferred weight)
        pass

    d_uni = distance_uni_km if distance_uni_km is not None else 99.0
    d_center = distance_center_km if distance_center_km is not None else 99.0
    # Effective distance: prefer uni but accept center
    effective = min(d_uni, d_center + 0.5)
    if effective <= 2:
        pts = 30.0
    elif effective <= 4:
        pts = 24.0
    elif effective <= 6:
        pts = 16.0
    elif effective <= 10:
        pts = 8.0
    else:
        pts = 2.0
    if distance_uni_km is not None:
        label = f"{distance_uni_km:.1f} km to uni"
        if distance_center_km is not None:
            label += f", {distance_center_km:.1f} km to center"
    else:
        label = f"{distance_center_km:.1f} km to center"
    return pts, label


def score_listing(listing: dict) -> tuple[float, dict]:
    """Compute match score 0–100-ish and breakdown."""
    text = " ".join(
        filter(
            None,
            [
                listing.get("title") or "",
                listing.get("description") or "",
                " ".join(listing.get("amenities") or []),
                listing.get("address") or "",
            ],
        )
    )
    features = detect_features(text)
    # Apply explicit flags if set
    for key in ("balcony", "sofa", "furnished", "parking", "own_bathroom"):
        if listing.get(key) is not None:
            features[key] = bool(listing.get(key))

    price_pts, price_note = score_price(listing.get("price"))
    dist_pts, dist_note = score_distance(
        listing.get("distance_uni_km"), listing.get("distance_center_km")
    )
    kw_pts, kw_hits = keyword_bonus(text)

    feature_pts = 0.0
    feature_notes = []
    if features.get("own_bathroom", True):
        feature_pts += 10.0
        feature_notes.append("own bathroom")
    else:
        feature_pts -= 25.0
        feature_notes.append("shared bathroom")
    if features.get("balcony"):
        feature_pts += 8.0
        feature_notes.append("balcony")
    if features.get("sofa"):
        feature_pts += 5.0
        feature_notes.append("sofa")
    if features.get("furnished"):
        feature_pts += 7.0
        feature_notes.append("furnished")

    shared_penalty = 0.0
    if features.get("is_shared") or (
        listing.get("rooms") is not None and listing.get("rooms", 1) < 1
    ):
        # room-only listings
        title = (listing.get("title") or "").lower()
        if "zimmer" in title and "wohnung" not in title and "apartment" not in title:
            shared_penalty = -35.0
            feature_notes.append("looks like shared room")

    # Size / rooms bonus for having own place
    size_pts = 0.0
    size = listing.get("size_sqm")
    rooms = listing.get("rooms")
    if size and size >= 20:
        size_pts += 5.0
    if rooms and rooms >= 1:
        size_pts += 5.0

    # Availability near Sept 2026
    avail_pts = 0.0
    avail = (listing.get("available_from") or "").lower()
    if avail in ("sofort", "ab sofort", "immediately", ""):
        avail_pts = 3.0
    elif re.search(r"2026-0[89]|09[./-]0?1|01[./-]09|september", avail):
        avail_pts = 8.0

    total = (
        price_pts
        + dist_pts
        + kw_pts
        + feature_pts
        + size_pts
        + avail_pts
        + shared_penalty
    )
    # Normalize roughly into 0–100
    total = max(0.0, min(100.0, total))

    breakdown = {
        "price": {"points": round(price_pts, 1), "note": price_note},
        "distance": {"points": round(dist_pts, 1), "note": dist_note},
        "keywords": {"points": round(kw_pts, 1), "hits": kw_hits},
        "features": {"points": round(feature_pts, 1), "notes": feature_notes},
        "size_rooms": {"points": round(size_pts, 1)},
        "availability": {"points": round(avail_pts, 1), "from": listing.get("available_from")},
        "shared_penalty": round(shared_penalty, 1),
        "total": round(total, 1),
    }
    return round(total, 1), breakdown


def enrich_features(listing: dict) -> dict:
    """Fill boolean feature flags and term/tenancy labels from text if missing."""
    text = " ".join(
        filter(
            None,
            [
                listing.get("title") or "",
                listing.get("description") or "",
                " ".join(listing.get("amenities") or []),
            ],
        )
    )
    feats = detect_features(text)
    out = dict(listing)
    for key in ("balcony", "sofa", "furnished", "parking"):
        if out.get(key) is None:
            out[key] = feats[key]
        elif key not in out:
            out[key] = feats[key]
    if out.get("own_bathroom") is None:
        out["own_bathroom"] = feats["own_bathroom"]

    labels = detect_term_tenancy(text, out.get("rental_duration"))
    # Always refresh from current text so rescrapes improve labels
    if labels.get("term_type"):
        out["term_type"] = labels["term_type"]
    elif "term_type" not in out:
        out["term_type"] = None
    if labels.get("tenancy_type"):
        out["tenancy_type"] = labels["tenancy_type"]
    elif "tenancy_type" not in out:
        out["tenancy_type"] = None
    return out


def apply_distances(listing: dict) -> dict:
    out = dict(listing)
    lat, lon = out.get("lat"), out.get("lon")
    if lat is not None and lon is not None:
        out["distance_uni_km"] = round(
            haversine_km(lat, lon, UNI_AUGSBURG["lat"], UNI_AUGSBURG["lon"]), 2
        )
        out["distance_center_km"] = round(
            haversine_km(lat, lon, CITY_CENTER["lat"], CITY_CENTER["lon"]), 2
        )
    return out


def geocode_address(address: str, city: str = "Augsburg") -> tuple[Optional[float], Optional[float]]:
    """Geocode via OpenStreetMap Nominatim (rate-limited)."""
    global _LAST_GEOCODE_TS
    if not address:
        return None, None
    query = address if "augsburg" in address.lower() else f"{address}, {city}, Germany"
    cache_key = query.strip().lower()
    if cache_key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[cache_key]

    # Nominatim: max 1 req/sec
    elapsed = time.time() - _LAST_GEOCODE_TS
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)

    url = (
        "https://nominatim.openstreetmap.org/search"
        f"?q={quote(query)}&format=json&limit=1&countrycodes=de"
    )
    try:
        with httpx.Client(timeout=20.0, headers={"User-Agent": USER_AGENT}) as client:
            resp = client.get(url)
            _LAST_GEOCODE_TS = time.time()
            if resp.status_code != 200:
                _GEOCODE_CACHE[cache_key] = (None, None)
                return None, None
            data = resp.json()
            if not data:
                # try district-only fallback
                fallback = f"{city}, Germany"
                if cache_key != fallback.lower():
                    _GEOCODE_CACHE[cache_key] = (None, None)
                return None, None
            lat = float(data[0]["lat"])
            lon = float(data[0]["lon"])
            _GEOCODE_CACHE[cache_key] = (lat, lon)
            return lat, lon
    except Exception:
        _GEOCODE_CACHE[cache_key] = (None, None)
        return None, None


def prepare_listing(listing: dict, *, do_geocode: bool = True) -> dict:
    """Enrich features, resolve map coordinates, compute distances and score."""
    from pipeline.geo import resolve_coordinates

    out = enrich_features(listing)
    if do_geocode or out.get("lat") is None or out.get("lon") is None:
        lat, lon, precision = resolve_coordinates(out, do_nominatim=do_geocode)
        out["lat"] = lat
        out["lon"] = lon
        out["geo_precision"] = precision
    out = apply_distances(out)
    score, breakdown = score_listing(out)
    out["match_score"] = score
    out["score_breakdown"] = breakdown
    return out
