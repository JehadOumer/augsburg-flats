"""Immowelt.de apartment rentals scraper (Augsburg)."""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from pipeline import db
from pipeline.scrapers.base import BaseScraper, abs_url, normalize_image_url, parse_float, parse_price

logger = logging.getLogger(__name__)

SEARCH_URL = (
    "https://www.immowelt.de/suche/augsburg/wohnungen/mieten"
    "?mmi=400&mma=900&rfr=1&sorting=Relevancy"
)
MAX_LISTINGS = 120
# Exposé pages hold the full carousel; list cards only expose the title shot.
DETAIL_ENRICH_LIMIT = 100
_MMS_RE = re.compile(r"https://mms\.immowelt\.de/[^\s\"'\\<>]+", re.I)


class ImmoweltScraper(BaseScraper):
    source = "immowelt"
    base_url = "https://www.immowelt.de"
    _session_warmed = False

    def scrape(self) -> list[dict]:
        listings: list[dict] = []
        seen_urls: set[str] = set()
        for page in range(1, 5):
            page_url = SEARCH_URL if page == 1 else f"{SEARCH_URL}&page={page}"
            try:
                resp = self.fetch(page_url)
                resp.raise_for_status()
                self._session_warmed = True
                soup = BeautifulSoup(resp.text, "lxml")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Immowelt page %s failed: %s", page, exc)
                break

            page_items = self._from_json_ld(soup) or self._from_cards(soup) or self._from_next_data(soup)
            new_on_page = 0
            for item in page_items:
                if item["url"] not in seen_urls:
                    seen_urls.add(item["url"])
                    listings.append(item)
                    new_on_page += 1
            if new_on_page == 0 or len(listings) >= MAX_LISTINGS:
                break
        listings = listings[:MAX_LISTINGS]
        self._enrich_sparse_galleries(listings)
        return listings

    def _warm_session(self) -> None:
        """Hit a search page first — bare exposé fetches often get 403."""
        if self._session_warmed:
            return
        try:
            resp = self.fetch(SEARCH_URL)
            if resp.status_code == 200:
                self._session_warmed = True
        except Exception as exc:  # noqa: BLE001
            logger.debug("Immowelt warm failed: %s", exc)

    @staticmethod
    def _extract_gallery_from_html(html: str) -> list[str]:
        """Collect unique mms.immowelt.de carousel photos from an exposé page."""
        found: list[str] = []
        seen: set[str] = set()
        for raw in _MMS_RE.findall(html):
            url = raw.replace("\\u002F", "/").replace("\\/", "/").rstrip("\\")
            # Drop truncated / non-image tokens
            base = url.split("?")[0].lower()
            if not base.endswith((".jpg", ".jpeg", ".png", ".webp")):
                continue
            if base in seen:
                continue
            seen.add(base)
            found.append(url)
            if len(found) >= 20:
                break
        return found

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
        """Fetch exposé page and replace title-thumb with full gallery.

        Returns True when the response looks blocked.
        """
        self._warm_session()
        resp = self.fetch(item["url"])
        if resp.status_code in (403, 429):
            return True
        if resp.status_code != 200:
            return False
        photos = self._extract_gallery_from_html(resp.text)
        if len(photos) >= 2:
            item["image_urls"] = photos
        elif photos and len(item.get("image_urls") or []) < 1:
            item["image_urls"] = photos

        # Prefer a longer description when the list card only had a teaser
        soup = BeautifulSoup(resp.text, "lxml")
        for sel in (
            "[data-testid='object-description']",
            "[class*='Description']",
            "#objectDescription",
            "section[class*='description']",
        ):
            el = soup.select_one(sel)
            if el:
                txt = el.get_text("\n", strip=True)
                if len(txt) > len(item.get("description") or ""):
                    item["description"] = txt[:3000]
                break
        return False

    def _enrich_sparse_galleries(self, listings: list[dict]) -> None:
        """Fetch exposé pages for cards that still only have a title photo."""
        # Prefer listings that need photos most (0–1 images, not already rich in DB)
        candidates = sorted(
            listings,
            key=lambda it: (len(it.get("image_urls") or []), self._stored_gallery_size(it.get("url") or "")),
        )
        fetches = 0
        upgraded = 0
        consecutive_blocks = 0
        for item in candidates:
            if fetches >= DETAIL_ENRICH_LIMIT:
                break
            current = len(item.get("image_urls") or [])
            stored = self._stored_gallery_size(item.get("url") or "")
            if current >= 3 or stored >= 3:
                continue
            fetches += 1
            try:
                blocked = self._enrich_detail(item)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Immowelt detail failed %s: %s", item.get("url"), exc)
                continue
            if blocked:
                consecutive_blocks += 1
                if consecutive_blocks >= 3:
                    logger.warning(
                        "Immowelt blocking exposé fetches after %s tries — stopping enrichment",
                        fetches,
                    )
                    break
                continue
            consecutive_blocks = 0
            if len(item.get("image_urls") or []) >= 2:
                upgraded += 1
        if fetches:
            logger.info(
                "Immowelt gallery enrich: fetched %s exposés, upgraded %s galleries",
                fetches,
                upgraded,
            )

    def _from_json_ld(self, soup: BeautifulSoup) -> list[dict]:
        out = []
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or "")
            except Exception:  # noqa: BLE001
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get("@type") in (
                    "Apartment",
                    "Product",
                    "RealEstateListing",
                    "Residence",
                ):
                    parsed = self._from_ld_item(item)
                    if parsed:
                        out.append(parsed)
                if isinstance(item, dict) and "itemListElement" in item:
                    for el in item["itemListElement"]:
                        obj = el.get("item", el) if isinstance(el, dict) else None
                        if isinstance(obj, dict):
                            parsed = self._from_ld_item(obj)
                            if parsed:
                                out.append(parsed)
        return out

    def _from_ld_item(self, item: dict) -> Optional[dict]:
        url = item.get("url") or item.get("@id")
        if not url:
            return None
        url = abs_url(self.base_url, url)
        title = item.get("name") or item.get("title") or "Wohnung Augsburg"
        price = None
        offers = item.get("offers") or {}
        if isinstance(offers, dict):
            price = parse_price(str(offers.get("price", "")))
        address = ""
        addr = item.get("address")
        if isinstance(addr, dict):
            address = ", ".join(
                filter(
                    None,
                    [
                        addr.get("streetAddress"),
                        addr.get("addressLocality") or "Augsburg",
                    ],
                )
            )
        return {
            "source": self.source,
            "url": url.split("?")[0],
            "title": title,
            "description": (item.get("description") or "")[:1500],
            "price": price,
            "address": address or "Augsburg",
            "city": "Augsburg",
            "image_urls": self._images_from_value(item.get("image")),
            "status": "active",
        }

    @staticmethod
    def _images_from_value(val) -> list[str]:
        out: list[str] = []
        if isinstance(val, str) and val:
            out.append(val)
        elif isinstance(val, list):
            for entry in val:
                if isinstance(entry, str) and entry:
                    out.append(entry)
                elif isinstance(entry, dict):
                    u = entry.get("url") or entry.get("uri") or entry.get("contentUrl") or ""
                    if u:
                        out.append(u)
        return out[:20]

    def _from_next_data(self, soup: BeautifulSoup) -> list[dict]:
        out = []
        script = soup.select_one("script#__NEXT_DATA__")
        if not script or not script.string:
            return out
        try:
            data = json.loads(script.string)
        except Exception:  # noqa: BLE001
            return out

        def walk(obj):
            if isinstance(obj, dict):
                # look for estate lists
                if "items" in obj and isinstance(obj["items"], list):
                    for it in obj["items"]:
                        if isinstance(it, dict) and ("price" in it or "title" in it or "name" in it):
                            parsed = self._from_api_item(it)
                            if parsed:
                                out.append(parsed)
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v)

        walk(data)
        return out

    def _from_api_item(self, it: dict) -> Optional[dict]:
        url = it.get("url") or it.get("detailUrl") or it.get("seoUrl")
        if not url:
            oid = it.get("id") or it.get("onlineId")
            if oid:
                url = f"https://www.immowelt.de/expose/{oid}"
            else:
                return None
        url = abs_url(self.base_url, url)
        title = it.get("title") or it.get("name") or it.get("headline") or "Wohnung Augsburg"
        price = None
        for key in ("price", "priceValue", "coldRent", "warmRent", "monthlyPrice"):
            if key in it and it[key] is not None:
                price = parse_price(str(it[key]))
                if price:
                    break
        if price is None and isinstance(it.get("prices"), dict):
            price = parse_price(str(it["prices"].get("primary") or it["prices"].get("value") or ""))

        rooms = parse_float(str(it.get("rooms") or it.get("numberOfRooms") or ""))
        size = parse_float(str(it.get("livingSpace") or it.get("area") or it.get("size") or ""))
        address = it.get("address") or it.get("location") or ""
        if isinstance(address, dict):
            address = ", ".join(
                filter(None, [address.get("street"), address.get("district"), address.get("city") or "Augsburg"])
            )
        district = it.get("district") or it.get("cityQuarter")
        images: list[str] = []
        for img_key in ("image", "titlePicture", "pictures", "images"):
            for u in self._images_from_value(it.get(img_key)):
                if u not in images:
                    images.append(u)

        return {
            "source": self.source,
            "external_id": str(it.get("id") or it.get("onlineId") or ""),
            "url": url.split("?")[0],
            "title": str(title)[:200],
            "description": str(it.get("description") or it.get("teaser") or "")[:1500],
            "price": price,
            "rooms": rooms,
            "size_sqm": size,
            "address": str(address) if address else "Augsburg",
            "district": str(district) if district else None,
            "city": "Augsburg",
            "image_urls": images[:20],
            "status": "active",
        }

    def _from_cards(self, soup: BeautifulSoup) -> list[dict]:
        out = []
        seen = set()
        for a in soup.select("a[href*='/expose/'], a[href*='/expose']"):
            href = a.get("href", "")
            url = abs_url(self.base_url, href)
            if not url or "/expose/" not in url:
                continue
            url = url.split("?")[0]
            if url in seen:
                continue
            seen.add(url)
            card = a.find_parent(["div", "article", "li"])
            blob = card.get_text(" ", strip=True) if card else a.get_text(" ", strip=True)
            title = a.get_text(" ", strip=True) or "Wohnung Augsburg"
            if card:
                h = card.select_one("h2, h3, [class*='headline'], [class*='title']")
                if h:
                    title = h.get_text(" ", strip=True)
            price = None
            m = re.search(r"(\d+[.,]?\d*)\s*€", blob)
            if m:
                price = parse_price(m.group(1))
            rooms = size = None
            m = re.search(r"(\d+[.,]?\d*)\s*Zi", blob, re.I)
            if m:
                rooms = parse_float(m.group(1))
            m = re.search(r"(\d+[.,]?\d*)\s*m²", blob)
            if m:
                size = parse_float(m.group(1))
            img = card.select_one("img") if card else None
            images = []
            if img:
                for attr in ("data-src", "data-srcset", "srcset", "src"):
                    nu = normalize_image_url(img.get(attr), self.base_url)
                    if nu:
                        images.append(nu)
                        break
            out.append(
                {
                    "source": self.source,
                    "url": url,
                    "title": title[:200],
                    "description": blob[:400],
                    "price": price,
                    "rooms": rooms,
                    "size_sqm": size,
                    "address": "Augsburg",
                    "city": "Augsburg",
                    "image_urls": images,
                    "status": "active",
                }
            )
        return out


class ImmonetScraper(ImmoweltScraper):
    """Immonet merged into Immowelt — its searches redirect to
    immowelt.de/classified-search, so scrape that (it can surface listings
    the classic /suche/ page does not)."""

    source = "immonet"
    base_url = "https://www.immowelt.de"

    def scrape(self) -> list[dict]:
        base_search = (
            "https://www.immowelt.de/classified-search"
            "?distributionTypes=Rent&estateTypes=Apartment"
            "&locations=AD08DE8634&priceMax=900"
        )
        listings: list[dict] = []
        seen: set[str] = set()
        for page in range(1, 4):
            page_url = base_search if page == 1 else f"{base_search}&page={page}"
            try:
                resp = self.fetch(page_url)
                resp.raise_for_status()
                self._session_warmed = True
                soup = BeautifulSoup(resp.text, "lxml")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Immonet page %s failed: %s", page, exc)
                break

            page_items = self._from_json_ld(soup) or self._from_cards(soup) or self._from_next_data(soup)
            new_on_page = 0
            for item in page_items:
                if item["url"] not in seen:
                    seen.add(item["url"])
                    item["source"] = self.source
                    listings.append(item)
                    new_on_page += 1
            if new_on_page == 0 or len(listings) >= 100:
                break
        listings = listings[:100]
        self._enrich_sparse_galleries(listings)
        return listings
