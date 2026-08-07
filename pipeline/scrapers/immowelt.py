"""Immowelt.de apartment rentals scraper (Augsburg)."""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from pipeline.scrapers.base import BaseScraper, abs_url, extract_images_from_soup, normalize_image_url, parse_float, parse_price

logger = logging.getLogger(__name__)

SEARCH_URL = (
    "https://www.immowelt.de/suche/augsburg/wohnungen/mieten"
    "?mmi=400&mma=900&rfr=1&sorting=Relevancy"
)
MAX_LISTINGS = 120


class ImmoweltScraper(BaseScraper):
    source = "immowelt"
    base_url = "https://www.immowelt.de"

    def scrape(self) -> list[dict]:
        listings: list[dict] = []
        seen_urls: set[str] = set()
        for page in range(1, 5):
            page_url = SEARCH_URL if page == 1 else f"{SEARCH_URL}&page={page}"
            try:
                resp = self.fetch(page_url)
                resp.raise_for_status()
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
        return listings[:MAX_LISTINGS]

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
            "image_urls": [item["image"]] if isinstance(item.get("image"), str) else [],
            "status": "active",
        }

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
        images = []
        for img_key in ("image", "titlePicture", "pictures", "images"):
            val = it.get(img_key)
            if isinstance(val, str):
                images.append(val)
            elif isinstance(val, list) and val:
                first = val[0]
                if isinstance(first, str):
                    images.append(first)
                elif isinstance(first, dict):
                    images.append(first.get("url") or first.get("uri") or "")
        images = [i for i in images if i]

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
            "image_urls": images[:5],
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
        return listings[:100]
