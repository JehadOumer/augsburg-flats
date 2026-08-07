"""WG-Gesucht.de scraper — 1-room apartments / studios only (not shared rooms)."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

from bs4 import BeautifulSoup

from pipeline import db
from pipeline.scrapers.base import BaseScraper, abs_url, parse_float, parse_price

logger = logging.getLogger(__name__)

# URL format: <name>.<city_id>.<category>.<rent_type>.<page>.html
# Augsburg city_id = 2 (8 is Berlin!); category 2 = apartments, 1 = 1-room apartments
SEARCH_URLS = [
    f"https://www.wg-gesucht.de/wohnungen-in-Augsburg.2.2.1.{page}.html"
    for page in range(0, 3)
] + [
    f"https://www.wg-gesucht.de/1-zimmer-wohnungen-in-Augsburg.2.1.1.{page}.html"
    for page in range(0, 3)
]
MAX_LISTINGS = 120
DETAIL_FETCH_BUDGET = 60  # max detail-page fetches per run (politeness)
MAX_CONSECUTIVE_BLOCKS = 3  # abort enrichment when WG starts rate-limiting

# Gallery photos live on img.wg-gesucht.de/media/up/... in several size variants
_IMG_RE = re.compile(
    r"https://img\.wg-gesucht\.de/media/up/[^\"'\\\s<>]+?\.(?:jpe?g|png|webp)",
    re.I,
)
_SIZE_TOKEN_RE = re.compile(r"\.(?:small|thumb|medium|sized|large|original)(?=\.)", re.I)
_SIZE_RANK = {"original": 5, "large": 4, "sized": 3, "medium": 2, "small": 1, "thumb": 0}


class WGGesuchtScraper(BaseScraper):
    source = "wg_gesucht"
    base_url = "https://www.wg-gesucht.de"

    def scrape(self) -> list[dict]:
        listings: list[dict] = []
        seen = set()
        for page_url in SEARCH_URLS:
            try:
                soup = self.soup(page_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("WG-Gesucht page failed (%s): %s", page_url, exc)
                continue
            self._collect_from_page(soup, listings, seen)
            if len(listings) >= MAX_LISTINGS:
                break
        listings = listings[:MAX_LISTINGS]
        self._enrich_all(listings)
        return listings

    def _enrich_all(self, listings: list[dict]) -> None:
        """Enrich listings that still need full galleries, respecting rate limits.

        Listings whose stored gallery is already complete are skipped so the
        fetch budget goes to the ones that actually need photos.
        """
        fetches = 0
        consecutive_blocks = 0
        for item in listings:
            if fetches >= DETAIL_FETCH_BUDGET:
                break
            if self._stored_gallery_size(item["url"]) >= 3:
                continue
            fetches += 1
            time.sleep(0.5)  # extra politeness on top of BaseScraper.fetch
            try:
                blocked = self._enrich_detail(item)
            except Exception as exc:  # noqa: BLE001
                logger.debug("WG-Gesucht detail failed %s: %s", item.get("url"), exc)
                continue
            if blocked:
                consecutive_blocks += 1
                if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
                    logger.warning(
                        "WG-Gesucht rate limiting detected after %d fetches — stopping enrichment",
                        fetches,
                    )
                    break
                time.sleep(5)  # back off before the next attempt
            else:
                consecutive_blocks = 0

    @staticmethod
    def _stored_gallery_size(url: str) -> int:
        try:
            with db.get_conn() as conn:
                row = conn.execute(
                    "SELECT image_urls FROM listings WHERE url = ?", (url,)
                ).fetchone()
            if row and row["image_urls"]:
                return len(json.loads(row["image_urls"]))
        except Exception:  # noqa: BLE001
            pass
        return 0

    def _enrich_detail(self, item: dict) -> bool:
        """Fetch the detail page for ALL gallery photos + full description.

        Returns True when the response looks rate-limited/blocked.
        """
        resp = self.fetch(item["url"])
        if resp.status_code in (403, 429):
            return True
        if resp.status_code != 200:
            return False
        html = resp.text
        # Blocked/captcha wall: an ad page without its own media images
        if not _IMG_RE.search(html) and ("captcha" in html.lower() or "zu viele anfragen" in html.lower()):
            return True

        # Collect every gallery photo; dedupe size variants keeping the largest
        best: dict[str, tuple[int, str]] = {}
        order: list[str] = []
        for url in _IMG_RE.findall(html):
            m = _SIZE_TOKEN_RE.search(url)
            size = m.group(0)[1:].lower() if m else "sized"
            key = _SIZE_TOKEN_RE.sub("", url)
            rank = _SIZE_RANK.get(size, 3)
            if key not in best:
                order.append(key)
                best[key] = (rank, url)
            elif rank > best[key][0]:
                best[key] = (rank, url)
        photos = [best[k][1] for k in order][:15]
        if photos:
            item["image_urls"] = photos

        soup = BeautifulSoup(html, "lxml")

        # Full free-text description (sections have ids like freitext_0)
        parts = []
        for sec in soup.select("[id^=freitext], #ad_description_text"):
            txt = sec.get_text("\n", strip=True)
            if txt and len(txt) > 20:
                parts.append(txt)
        if parts:
            item["description"] = "\n\n".join(parts)[:3000]

        # Availability from the detail page is more reliable than the card
        body_text = soup.get_text(" ", strip=True)
        m = re.search(r"frei ab:?\s*(\d{2}\.\d{2}\.\d{4})", body_text, re.I)
        if m:
            d, mo, y = m.group(1).split(".")
            item["available_from"] = f"{y}-{mo}-{d}"

        # Exact street address if shown
        m = re.search(r"(?:Adresse|Anschrift)\s+([^\n|]{5,60}?)\s+86\d{3}\s+Augsburg", body_text)
        if m:
            street = m.group(1).strip(" ,")
            if street and street.lower() not in (item.get("address") or "").lower():
                item["address"] = f"{street}, Augsburg"
        return False

    def _collect_from_page(self, soup: BeautifulSoup, listings: list[dict], seen: set) -> int:
        new_count = 0
        cards = soup.select(".wgg_card")
        if cards:
            for card in cards:
                item = self._parse_card(card)
                if item and item["url"] not in seen:
                    seen.add(item["url"])
                    listings.append(item)
                    new_count += 1
                if len(listings) >= MAX_LISTINGS:
                    break
            return new_count

        # Fallback: older link-based markup
        for a in soup.select("a[href*='wohnungen-in-Augsburg'], a[href*='/wohnungen/']"):
            href = a.get("href", "")
            if re.search(r"\.\d+\.html", href):
                full = abs_url(self.base_url, href)
                if full and full not in seen:
                    card = a.find_parent("div", class_=re.compile(r"card|offer|row"))
                    item = self._parse_from_link(a, card)
                    if item and item["url"] not in seen:
                        if item.get("rooms") and item["rooms"] > 3:
                            continue
                        seen.add(item["url"])
                        listings.append(item)
                        new_count += 1
            if len(listings) >= MAX_LISTINGS:
                break
        return new_count

    def _parse_card(self, card) -> Optional[dict]:
        a = None
        for cand in card.select("a[href*='.html']"):
            href = cand.get("href", "")
            if re.search(r"\.(\d+)\.html$", href.split("?")[0]):
                a = cand
                break
        if not a:
            return None
        url = abs_url(self.base_url, a.get("href"))
        if not url:
            return None
        url = url.split("?")[0]

        blob = re.sub(r"\s+", " ", card.get_text(" ", strip=True))

        # Title: longest anchor text on the card
        title = ""
        for cand in card.select("a"):
            t = cand.get_text(" ", strip=True)
            if len(t) > len(title):
                title = t
        title = title or "Wohnung Augsburg"
        tl = title.lower()
        if "wg-zimmer" in tl or (tl.startswith("zimmer") and "wohnung" not in tl):
            return None

        price = None
        m = re.search(r"(\d[\d.,]*)\s*€", blob)
        if m:
            price = parse_price(m.group(1))

        size = rooms = None
        m = re.search(r"(\d+[.,]?\d*)\s*m²", blob)
        if m:
            size = parse_float(m.group(1))
        m = re.search(r"(\d+[.,]?\d*)[\s-]*Zimmer", blob, re.I)
        if m:
            rooms = parse_float(m.group(1))

        available_from = None
        m = re.search(r"Verfügbar:\s*(\d{2}\.\d{2}\.\d{4})", blob)
        if not m:
            m = re.search(r"(\d{2}\.\d{2}\.\d{4})", blob)
        if m:
            d, mo, y = m.group(1).split(".")
            available_from = f"{y}-{mo}-{d}"

        district = street = None
        m = re.search(
            r"\|\s*Augsburg\s+([A-ZÄÖÜ][\wäöüß./\- ]{2,30}?)\s*\|\s*([A-ZÄÖÜ][\wäöüß.\- ]+)?",
            blob,
        )
        if m:
            district = m.group(1).strip()
            street = (m.group(2) or "").strip() or None

        images = []
        img = card.select_one("img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src and "placeholder" not in src and not src.startswith("data:"):
                images.append(abs_url(self.base_url, src.replace(".small.", ".large.")))

        address_bits = [b for b in (street, district, "Augsburg") if b]

        ext = re.search(r"\.(\d+)\.html$", url)
        return {
            "source": self.source,
            "external_id": ext.group(1) if ext else None,
            "url": url,
            "title": title[:200],
            "description": blob[:500],
            "price": price,
            "rooms": rooms or 1,
            "size_sqm": size,
            "address": ", ".join(address_bits),
            "district": district,
            "city": "Augsburg",
            "available_from": available_from,
            "image_urls": [i for i in images if i],
            "amenities": [],
            "status": "active",
        }

    def _parse_from_link(self, a, card) -> Optional[dict]:
        href = a.get("href")
        url = abs_url(self.base_url, href)
        if not url or not re.search(r"\d+\.html", url):
            return None
        # Skip category listing pages
        if re.search(r"wohnungen-in-Augsburg\.\d+\.\d+", url):
            return None

        title = a.get_text(" ", strip=True) or "Wohnung Augsburg"
        if len(title) < 5 and card:
            t2 = card.select_one("h3, h4, .truncate_title")
            if t2:
                title = t2.get_text(" ", strip=True)

        blob = card.get_text(" ", strip=True) if card else title
        price = None
        m = re.search(r"(\d+[.,]?\d*)\s*€", blob)
        if m:
            price = parse_price(m.group(1))

        size = rooms = None
        m = re.search(r"(\d+[.,]?\d*)\s*m²", blob)
        if m:
            size = parse_float(m.group(1))
        m = re.search(r"(\d+[.,]?\d*)\s*(?:Zimmer|Zi\.?)", blob, re.I)
        if m:
            rooms = parse_float(m.group(1))

        available_from = None
        m = re.search(r"(?:frei ab|ab)\s*(\d{2}\.\d{2}\.\d{4})", blob, re.I)
        if m:
            d, mo, y = m.group(1).split(".")
            available_from = f"{y}-{mo}-{d}"

        district = None
        m = re.search(
            r"Augsburg\s*[|\-–]\s*([A-ZÄÖÜ][\w\-äöü]+(?:\s+[A-ZÄÖÜ][\w\-äöü]+)?)",
            blob,
        )
        if m:
            district = m.group(1).strip()

        img = card.select_one("img") if card else None
        images = []
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src and "placeholder" not in src and not src.startswith("data:"):
                images.append(abs_url(self.base_url, src))

        # Skip obvious shared rooms
        tl = title.lower()
        if "wg-zimmer" in tl or (tl.startswith("zimmer") and "wohnung" not in tl):
            return None

        return {
            "source": self.source,
            "external_id": re.search(r"(\d+)\.html", url).group(1) if re.search(r"(\d+)\.html", url) else None,
            "url": url.split("?")[0],
            "title": title[:200],
            "description": blob[:500] if card else "",
            "price": price,
            "rooms": rooms or 1,
            "size_sqm": size,
            "address": f"{district}, Augsburg" if district else "Augsburg",
            "district": district,
            "city": "Augsburg",
            "available_from": available_from,
            "image_urls": images,
            "amenities": [],
            "status": "active",
        }
