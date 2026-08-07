"""Public-transit travel times to the University of Augsburg (AVV tram/bus).

Uses the free Transitous / MOTIS planner (covers German GTFS including Augsburg).
Results are cached by rounded coordinates so district-approximate pins share routes.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

from pipeline.config import UNI_AUGSBURG, USER_AGENT

logger = logging.getLogger(__name__)

TRANSITOUS_PLAN = "https://api.transitous.org/api/v1/plan"
_CACHE: dict[tuple[float, float], Optional[dict[str, Any]]] = {}
_LAST_TS = 0.0
_MIN_INTERVAL = 0.35  # be polite to the free API


def _cache_key(lat: float, lon: float) -> tuple[float, float]:
    # ~100 m grid — enough for district pins, avoids duplicate API calls
    return (round(lat, 3), round(lon, 3))


def _summarize_legs(legs: list[dict]) -> str:
    """Pick a short label like 'Tram 2' or 'Bus 32 · Tram 3'."""
    bits: list[str] = []
    for leg in legs:
        mode = (leg.get("mode") or "").upper()
        if mode in ("WALK", "BICYCLE", "CAR"):
            continue
        name = (
            leg.get("routeShortName")
            or leg.get("route")
            or leg.get("displayName")
            or ""
        )
        name = str(name).strip()
        if mode == "TRAM":
            label = f"Tram {name}".strip() if name else "Tram"
        elif mode == "BUS":
            label = f"Bus {name}".strip() if name else "Bus"
        elif mode in ("SUBWAY", "METRO", "RAIL", "TRAIN"):
            label = f"{mode.title()} {name}".strip()
        else:
            label = name or mode.title()
        if label and label not in bits:
            bits.append(label)
        if len(bits) >= 2:
            break
    return " · ".join(bits) if bits else "Transit"


def transit_to_uni(lat: float, lon: float) -> Optional[dict[str, Any]]:
    """Return {minutes, transfers, summary} for best itinerary, or None."""
    global _LAST_TS
    if lat is None or lon is None:
        return None
    key = _cache_key(float(lat), float(lon))
    if key in _CACHE:
        return _CACHE[key]

    # Skip absurdly far outliers (outside metro Augsburg)
    from pipeline.scoring import haversine_km

    if haversine_km(lat, lon, UNI_AUGSBURG["lat"], UNI_AUGSBURG["lon"]) > 40:
        _CACHE[key] = None
        return None

    elapsed = time.time() - _LAST_TS
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)

    params = {
        "fromPlace": f"{lat},{lon}",
        "toPlace": f"{UNI_AUGSBURG['lat']},{UNI_AUGSBURG['lon']}",
        "arriveBy": "false",
        "numItineraries": 3,
    }
    try:
        with httpx.Client(timeout=25.0, headers={"User-Agent": USER_AGENT}) as client:
            resp = client.get(TRANSITOUS_PLAN, params=params)
            _LAST_TS = time.time()
            if resp.status_code != 200:
                logger.debug("Transitous HTTP %s", resp.status_code)
                _CACHE[key] = None
                return None
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Transitous failed: %s", exc)
        _CACHE[key] = None
        return None

    itineraries = data.get("itineraries") or []
    if not itineraries:
        # Pure walk sometimes lands in "direct"
        direct = data.get("direct") or []
        if direct:
            dur = direct[0].get("duration")
            if dur is not None:
                result = {
                    "minutes": max(1, int(round(dur / 60))),
                    "transfers": 0,
                    "summary": "Walk",
                }
                _CACHE[key] = result
                return result
        _CACHE[key] = None
        return None

    # Prefer itineraries that actually use transit; among those, shortest time
    def score(it: dict) -> tuple:
        legs = it.get("legs") or []
        has_transit = any(
            (lg.get("mode") or "").upper() not in ("WALK", "BICYCLE", "CAR", "")
            for lg in legs
        )
        return (0 if has_transit else 1, it.get("duration") or 10**9)

    best = min(itineraries, key=score)
    minutes = max(1, int(round((best.get("duration") or 0) / 60)))
    transfers = int(best.get("transfers") or 0)
    summary = _summarize_legs(best.get("legs") or [])
    result = {"minutes": minutes, "transfers": transfers, "summary": summary}
    _CACHE[key] = result
    return result
