"""Wohnungsboerse.net Augsburg scraper."""

from __future__ import annotations

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from pipeline.scrapers.base import (
    BaseScraper,
    abs_url,
    extract_gallery_images,
    normalize_image_url,
    parse_float,
    parse_price,
)

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.wohnungsboerse.net/Augsburg/Wohnung-mieten/7009"
DETAIL_ENRICH_LIMIT = 25


class WohnungsboerseScraper(BaseScraper):
    source = "wohnungsboerse"
    base_url = "https://www.wohnungsboerse.net"

    def scrape(self) -> list[dict]:
        listings: list[dict] = []
        base_urls = [
            SEARCH_URL,
            "https://www.wohnungsboerse.net/mieten/Wohnung/Augsburg",
            "https://www.wohnungsboerse.net/suche?stadt=Augsburg&objektart=wohnung&nutzungsart=miete&preis_max=900",
        ]
        soups: list[BeautifulSoup] = []
        for u in base_urls:
            got_first = False
            for page in range(1, 4):
                page_url = u if page == 1 else f"{u}{'&' if '?' in u else '?'}page={page}"
                try:
                    resp = self.fetch(page_url)
                    if resp.status_code == 200:
                        soups.append(BeautifulSoup(resp.text, "lxml"))
                        got_first = True
                    else:
                        break
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Wohnungsboerse URL failed %s: %s", page_url, exc)
                    break
            if got_first:
                break
        if not soups:
            logger.warning("Wohnungsboerse search failed for all URLs")
            return listings

        seen = set()
        for soup in soups:
            self._collect(soup, listings, seen)
        for item in listings[:DETAIL_ENRICH_LIMIT]:
            try:
                self._enrich_detail(item)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Wohnungsboerse detail failed %s: %s", item.get("url"), exc)
        return listings

    def _enrich_detail(self, item: dict) -> None:
        """Fetch the exposé page for the photo gallery and a fuller description."""
        resp = self.fetch(item["url"])
        if resp.status_code != 200:
            return
        soup = BeautifulSoup(resp.text, "lxml")
        photos = extract_gallery_images(
            soup,
            self.base_url,
            own_url=item["url"],
            listing_link_fragment="/immo/",
            limit=15,
        )
        if photos:
            item["image_urls"] = photos
        desc = soup.select_one("[class*='description'], #description, .expose-text")
        if desc:
            txt = desc.get_text("\n", strip=True)
            if len(txt) > len(item.get("description") or ""):
                item["description"] = txt[:3000]
        # Street/postcode often shown on the exposé
        m = re.search(r"(86\d{3})\s+Augsburg", soup.get_text(" ", strip=True))
        if m and "86" not in (item.get("address") or ""):
            item["address"] = f"{m.group(1)} Augsburg"

    def _collect(self, soup: BeautifulSoup, listings: list[dict], seen: set) -> None:
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if not any(x in href for x in ("/immo/", "/expose/", "/objekt/", "/wohnung/", "wohnungsboerse.net")):
                # keep relative listing-like paths
                if not re.search(r"/\d{5,}", href):
                    continue
            full = abs_url(self.base_url, href)
            if not full or "wohnungsboerse.net" not in full:
                continue
            # skip nav / search pages
            if any(
                x in full.lower()
                for x in ("/suche", "/mieten/wohnung/augsburg", "login", "register", "javascript")
            ):
                if re.search(r"/immo/\d+|/\d{6,}", full):
                    pass
                else:
                    continue
            if not re.search(r"/\d{5,}", full):
                continue
            full = full.split("?")[0]
            if full in seen:
                continue
            seen.add(full)

            card = a.find_parent(["div", "article", "li", "tr"])
            blob = card.get_text(" ", strip=True) if card else a.get_text(" ", strip=True)
            title = a.get_text(" ", strip=True)
            if card:
                h = card.select_one("h2, h3, h4, .title, .headline")
                if h:
                    title = h.get_text(" ", strip=True)
            if not title or len(title) < 4:
                title = "Wohnung Augsburg"

            price = None
            m = re.search(r"(\d+[.,]?\d*)\s*€", blob)
            if m:
                price = parse_price(m.group(1))
            rooms = size = None
            m = re.search(r"(\d+[.,]?\d*)\s*(?:Zimmer|Zi\.?)", blob, re.I)
            if m:
                rooms = parse_float(m.group(1))
            m = re.search(r"(\d+[.,]?\d*)\s*m²", blob)
            if m:
                size = parse_float(m.group(1))

            images = []
            if card:
                img = card.select_one("img")
                if img:
                    for attr in ("data-src", "srcset", "src"):
                        nu = normalize_image_url(img.get(attr), self.base_url)
                        if nu:
                            images.append(nu)
                            break

            listings.append(
                {
                    "source": self.source,
                    "url": full,
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
            if len(listings) >= 90:
                break
