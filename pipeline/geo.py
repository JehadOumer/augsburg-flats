"""Resolve map coordinates for every listing (exact → district → city)."""

from __future__ import annotations

import hashlib
import re
from typing import Optional

from pipeline.config import CITY_CENTER, UNI_AUGSBURG
from pipeline.scoring import geocode_address

# Approximate centroids for Augsburg districts / neighbourhoods (WGS84).
# Used when a street address is missing but the listing mentions the area.
AUGSBURG_DISTRICTS: dict[str, tuple[float, float]] = {
    # Core / center
    "innenstadt": (48.3668, 10.8987),
    "stadtmitte": (48.3668, 10.8987),
    "city": (48.3668, 10.8987),
    "zentrum": (48.3668, 10.8987),
    "altstadt": (48.3695, 10.8980),
    "rathausplatz": (48.3690, 10.8987),
    # University / south-east
    "univiertel": (48.3345, 10.8974),
    "universität": (48.3345, 10.8974),
    "universitaet": (48.3345, 10.8974),
    "uniklinik": (48.3860, 10.8365),
    "uniklinikum": (48.3860, 10.8365),
    # Named Stadtteile
    "göggingen": (48.3445, 10.8705),
    "goeggingen": (48.3445, 10.8705),
    "pfersee": (48.3630, 10.8620),
    "lechhausen": (48.3780, 10.9250),
    "haunstetten": (48.3160, 10.9050),
    "hochfeld": (48.3540, 10.9050),
    "kriegshaber": (48.3750, 10.8600),
    "oberhausen": (48.3850, 10.8800),
    "bärenkeller": (48.3920, 10.8700),
    "baerenkeller": (48.3920, 10.8700),
    "hammererschule": (48.3550, 10.8850),
    "antonsviertel": (48.3600, 10.8920),
    "jakober": (48.3705, 10.9050),
    "jakobervorstadt": (48.3705, 10.9050),
    "st. ulrich": (48.3720, 10.8980),
    "ulrichsviertel": (48.3720, 10.8980),
    "fuggerviertel": (48.3680, 10.9020),
    "textilviertel": (48.3655, 10.9150),
    "proviantbach": (48.3725, 10.9180),
    "spickel": (48.3580, 10.9200),
    "firnhaberau": (48.3950, 10.9300),
    "hammerschmiede": (48.4050, 10.9150),
    "inningen": (48.3100, 10.8600),
    "bergheim": (48.3000, 10.8500),
    "gert": (48.3400, 10.8400),  # rare
    "herrenbach": (48.3500, 10.9250),
    "spickel-herrenbach": (48.3520, 10.9220),
    "lechnausen": (48.3780, 10.9250),  # typo variant
    "neusäß": (48.3960, 10.8250),
    "neusaess": (48.3960, 10.8250),
    "stadtbergen": (48.3650, 10.8450),
    "friedberg": (48.3570, 10.9850),
    "königsbrunn": (48.2700, 10.8900),
    "koenigsbrunn": (48.2700, 10.8900),
    "bobingen": (48.2700, 10.8300),
    "gersthofen": (48.4240, 10.8750),
    # Streets / landmarks often in HC24 text
    "max-reger": (48.3445, 10.8705),
    "bergstraße": (48.3480, 10.8750),
    "bergstrasse": (48.3480, 10.8750),
}

# PLZ → approximate Augsburg-area point
AUGSBURG_PLZ: dict[str, tuple[float, float]] = {
    "86150": (48.3668, 10.8987),  # Innenstadt
    "86152": (48.3700, 10.8950),
    "86153": (48.3655, 10.9150),  # Textilviertel / east
    "86154": (48.3780, 10.9250),  # Lechhausen
    "86156": (48.3750, 10.8600),  # Kriegshaber
    "86157": (48.3630, 10.8620),  # Pfersee
    "86159": (48.3345, 10.8974),  # Univiertel
    "86161": (48.3540, 10.9050),  # Hochfeld
    "86163": (48.3160, 10.9050),  # Haunstetten
    "86165": (48.3950, 10.9300),
    "86167": (48.3850, 10.8800),
    "86169": (48.3445, 10.8705),  # Göggingen-ish
    "86179": (48.3445, 10.8705),  # Göggingen
    "86356": (48.2700, 10.8900),  # Königsbrunn
    "86368": (48.4240, 10.8750),  # Gersthofen
    "86391": (48.3570, 10.9850),  # Stadtbergen/Friedberg area
    "86399": (48.2700, 10.8300),  # Bobingen
    "86415": (48.3960, 10.8250),  # Neusäß
}


def _norm(text: str) -> str:
    return (
        (text or "")
        .lower()
        .replace("ö", "oe")
        .replace("ä", "ae")
        .replace("ü", "ue")
        .replace("ß", "ss")
    )


def _jitter(seed: str, lat: float, lon: float, scale: float = 0.0018) -> tuple[float, float]:
    """Tiny deterministic offset so multiple listings in one district don't stack."""
    h = hashlib.md5(seed.encode("utf-8", errors="ignore")).hexdigest()
    dx = (int(h[:4], 16) / 65535.0 - 0.5) * 2 * scale
    dy = (int(h[4:8], 16) / 65535.0 - 0.5) * 2 * scale
    return round(lat + dx, 6), round(lon + dy, 6)


def extract_location_hints(listing: dict) -> list[str]:
    """Collect candidate place strings from listing fields / text."""
    hints: list[str] = []
    for key in ("district", "address", "title", "description"):
        val = listing.get(key)
        if val and isinstance(val, str):
            cleaned = re.sub(r"\s+", " ", val).strip()
            if cleaned:
                hints.append(cleaned)
    # postal codes
    blob = " ".join(hints)
    for m in re.finditer(r"\b(86\d{3})\b", blob):
        hints.insert(0, m.group(1))
    return hints


def match_district(text: str) -> Optional[tuple[str, float, float]]:
    t = _norm(text)
    # longer keys first to prefer specific matches
    for name, (lat, lon) in sorted(AUGSBURG_DISTRICTS.items(), key=lambda kv: -len(kv[0])):
        key = _norm(name)
        if key in t:
            return name, lat, lon
    return None


def match_plz(text: str) -> Optional[tuple[str, float, float]]:
    for m in re.finditer(r"\b(86\d{3})\b", text or ""):
        plz = m.group(1)
        if plz in AUGSBURG_PLZ:
            lat, lon = AUGSBURG_PLZ[plz]
            return plz, lat, lon
    return None


def resolve_coordinates(
    listing: dict,
    *,
    do_nominatim: bool = True,
) -> tuple[float, float, str]:
    """Return (lat, lon, precision) where precision is exact|district|plz|city.

    Always returns coordinates — never leaves a listing without a map point.
    """
    # 1) Already has coords
    if listing.get("lat") is not None and listing.get("lon") is not None:
        try:
            return float(listing["lat"]), float(listing["lon"]), listing.get("geo_precision") or "exact"
        except (TypeError, ValueError):
            pass

    seed = listing.get("url") or listing.get("title") or str(listing.get("id") or "x")
    hints = extract_location_hints(listing)
    blob = " ".join(hints)

    # 2) Known district / neighbourhood keyword
    dist = match_district(blob)
    if dist:
        name, lat, lon = dist
        lat, lon = _jitter(seed, lat, lon, scale=0.0022)
        return lat, lon, "district"

    # 3) Augsburg PLZ
    plz = match_plz(blob)
    if plz:
        name, lat, lon = plz
        lat, lon = _jitter(seed, lat, lon, scale=0.0025)
        return lat, lon, "plz"

    # 4) Nominatim on cleaned address / district (Augsburg-biased)
    if do_nominatim:
        candidates: list[str] = []
        addr = re.sub(r"\s+", " ", (listing.get("address") or "")).strip()
        district = re.sub(r"\s+", " ", (listing.get("district") or "")).strip()
        # Prefer short street-like addresses containing Augsburg
        if addr and len(addr) < 120:
            candidates.append(addr)
        if district and district.lower() not in (addr or "").lower():
            candidates.append(f"{district}, Augsburg, Germany")
        # title fragment with district words
        title = listing.get("title") or ""
        m = re.search(
            r"(Göggingen|Goeggingen|Pfersee|Lechhausen|Haunstetten|Hochfeld|"
            r"Kriegshaber|Oberhausen|Univiertel|Innenstadt|Textilviertel)",
            title,
            re.I,
        )
        if m:
            candidates.append(f"{m.group(1)}, Augsburg, Germany")

        for q in candidates:
            # Skip garbage with newlines / far-away towns mistakenly tagged Augsburg
            if re.search(r"\b(976\d{2}|Bad Neustadt|Niederlauer|Hohenroth|Sandberg)\b", q, re.I):
                continue
            lat, lon = geocode_address(q, listing.get("city") or "Augsburg")
            if lat is not None and lon is not None:
                # Sanity: must be roughly near Augsburg (within ~40km)
                from pipeline.scoring import haversine_km

                if haversine_km(lat, lon, CITY_CENTER["lat"], CITY_CENTER["lon"]) <= 40:
                    return lat, lon, "exact" if "," in q and any(c.isdigit() for c in q) else "district"

    # 5) City-center fallback (always)
    # Prefer uni if listing looks student-oriented
    text_l = blob.lower()
    if any(k in text_l for k in ("student", "uni", "campus", "univiertel")):
        lat, lon = _jitter(seed, UNI_AUGSBURG["lat"], UNI_AUGSBURG["lon"], scale=0.003)
        return lat, lon, "city"
    lat, lon = _jitter(seed, CITY_CENTER["lat"], CITY_CENTER["lon"], scale=0.004)
    return lat, lon, "city"
