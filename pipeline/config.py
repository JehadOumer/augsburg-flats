"""Search preferences and location anchors for the Augsburg apartment finder."""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "listings.db"
SITE_DIR = BASE_DIR / "site"
SITE_DATA_DIR = SITE_DIR / "data"
LISTINGS_JSON = SITE_DATA_DIR / "listings.json"
CONFIG_JSON = SITE_DATA_DIR / "config.json"

# Budget (EUR / month, warm rent preferred)
PRICE_IDEAL_MIN = 600
PRICE_IDEAL_MAX = 700
PRICE_HARD_MAX = 900

# Location anchors (WGS84)
UNI_AUGSBURG = {
    "name": "University of Augsburg",
    "lat": 48.3345,
    "lon": 10.8974,
    "address": "Universitätsstraße 2, 86159 Augsburg",
}
CITY_CENTER = {
    "name": "Augsburg City Center (Rathausplatz)",
    "lat": 48.3690,
    "lon": 10.8987,
}

# Move-in target
MOVE_IN_TARGET = "2026-09-01"

# Scrape interval (hours)
SCRAPE_INTERVAL_HOURS = 2

# Categories the user can assign
CATEGORIES = [
    "unreviewed",
    "shortlist",
    "contacted",
    "viewing",
    "rejected",
]

# Seed HC24 listings from the user's starting links
HC24_SEED_URLS = [
    "https://www.hc24.de/de/expose/au7951/",
    "https://www.hc24.de/de/expose/au7935/",
    "https://www.hc24.de/en/expose/au7946/",
    "https://www.hc24.de/en/expose/au7943/",
]

# Search keywords that boost score
NICE_TO_HAVE_KEYWORDS = [
    "balkon",
    "balcony",
    "sofa",
    "couch",
    "möbliert",
    "moebliert",
    "furnished",
    "einbauküche",
    "ebk",
    "waschmaschine",
    "washing machine",
    "wlan",
    "wifi",
    "internet",
    "parkplatz",
    "parking",
    "stellplatz",
]

# Keywords that indicate shared housing (penalize / filter)
SHARED_KEYWORDS = [
    "wg-zimmer",
    "wg zimmer",
    "room in shared",
    "mitbewohner",
    "shared flat room",
    "zwischenmiete zimmer",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Studentenwerk Augsburg (static resource, not scraped)
STUDENTENWERK_INFO = {
    "title": "Studentenwerk Augsburg – Student Housing",
    "url": "https://studentenwerk-augsburg.de/wohnen/",
    "notes": (
        "Official student residence waiting list. Apply early — waiting times "
        "can be long. Not scraped automatically; open the link to apply."
    ),
}

# Fallback search links when scraping is blocked
FALLBACK_SEARCH_LINKS = {
    "immobilienscout24": (
        "https://www.immobilienscout24.de/Suche/de/bayern/augsburg/wohnung-mieten"
        "?price=-900.0&numberofrooms=1.0-&livingspace=15.0-"
    ),
    "immosurf": "https://immosurf.de/mieten/wohnung/augsburg",
    "kleinanzeigen": (
        "https://www.kleinanzeigen.de/s-wohnung-mieten/augsburg/c203l6148"
    ),
    "wg_gesucht": (
        "https://www.wg-gesucht.de/wohnungen-in-Augsburg.8.2.1.0.html"
    ),
    "immowelt": (
        "https://www.immowelt.de/suche/augsburg/wohnungen/mieten"
        "?mmi=600&mma=900&rfr=1"
    ),
}
