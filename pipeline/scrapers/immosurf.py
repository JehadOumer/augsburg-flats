"""Immosurf.de aggregator scraper (Augsburg apartments).

Immosurf aggregates German rental listings. Search pages expose card data;
detail pages include schema.org RealEstateListing JSON-LD.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from pipeline.config import PRICE_HARD_MAX
from pipeline.scrapers.base import (
    BaseScraper,
    abs_url,
    extract_gallery_images,
    normalize_image_url,
    parse_float,
    parse_price,
)

logger = logging.getLogger(__name__)

SEARCH_URLS = [
    "https://immosurf.de/mieten/wohnung/augsburg",
    "https://immosurf.de/mieten/1-zimmer-wohnung/augsburg",
    "https://immosurf.de/mieten/augsburg",
]


class ImmosurfScraper(BaseScraper):
    source = "immosurf"
    base_url = "https://immosurf.de"

    def scrape(self) -> list[dict]:
        by_url: dict[str, dict] = {}
        for search in SEARCH_URLS:
            for page in range(1, 4):
                page_url = search if page == 1 else f"{search}?page={page}"
                try:
                    items = self._scrape_search(page_url)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Immosurf search failed %s: %s", page_url, exc)
                    break
                new_on_page = 0
                for item in items:
                    url = item.get("url")
                    if not url:
                        continue
                    # Prefer richer records (with description / more images)
                    prev = by_url.get(url)
                    if not prev:
                        new_on_page += 1
                    if not prev or len(item.get("description") or "") > len(prev.get("description") or ""):
                        by_url[url] = item
                if new_on_page == 0:
                    break

        # Enrich a subset with detail pages for better address/coords/images
        listings = list(by_url.values())
        enriched = []
        for item in listings[:60]:
            try:
                detail = self._enrich_detail(item)
                enriched.append(detail or item)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Immosurf detail failed %s: %s", item.get("url"), exc)
                enriched.append(item)
        # include remaining without detail fetch
        seen = {e["url"] for e in enriched}
        for item in listings[60:150]:
            if item["url"] not in seen:
                enriched.append(item)
        return enriched

    def _scrape_search(self, search_url: str) -> list[dict]:
        soup = self.soup(search_url)
        out: list[dict] = []
        seen = set()
        for a in soup.select("a[href*='/listings/']"):
            href = a.get("href") or ""
            if "/listings/" not in href:
                continue
            url = abs_url(self.base_url, href.split("?")[0])
            if not url or url in seen:
                continue
            # Skip non-Augsburg category pages accidentally matched
            if url.rstrip("/").endswith("/listings") or "/mieten/" in url:
                continue
            seen.add(url)

            card = a.find_parent(["div", "article", "li", "section", "a"])
            # Prefer outermost card with price text
            parent = a
            for _ in range(6):
                parent = parent.parent if parent else None
                if parent is None:
                    break
                txt = parent.get_text(" ", strip=True)
                if "€" in txt and ("m²" in txt or "m2" in txt or "Zimmer" in txt):
                    card = parent
                    break

            blob = card.get_text(" ", strip=True) if card else a.get_text(" ", strip=True)
            title = a.get_text(" ", strip=True) or "Wohnung Augsburg"
            # Prefer a clearer title from heading inside card
            if card:
                h = card.select_one("h2, h3, h4, [class*='title'], [class*='Title']")
                if h and len(h.get_text(strip=True)) > 5:
                    title = h.get_text(" ", strip=True)

            # Filter: skip pure WG rooms from "all" search if title says Zimmer only
            tl = title.lower()
            if ("wg" in tl or "zimmer zu vermieten" in tl) and "wohnung" not in tl and "apartment" not in tl:
                if re.search(r"\bwg\b", tl):
                    continue

            price = None
            m = re.search(r"(\d+[.,]?\d*)\s*€", blob)
            if m:
                price = parse_price(m.group(1))
            if price is not None and price > PRICE_HARD_MAX + 200:
                continue

            size = rooms = None
            m = re.search(r"(\d+[.,]?\d*)\s*m²", blob)
            if m:
                size = parse_float(m.group(1))
            m = re.search(r"(\d+[.,]?\d*)\s*(?:Zimmer|Zi\.?)", blob, re.I)
            if m:
                rooms = parse_float(m.group(1))

            address = "Augsburg"
            m = re.search(
                r"(\d{5}\s+Augsburg[^€]*?|[^,]+Augsburg[^,]*)",
                blob,
                re.I,
            )
            if m:
                address = m.group(1).strip(" ,|-")
            elif "Augsburg" in blob:
                # take snippet around Augsburg
                m = re.search(r"(.{0,40}Augsburg.{0,20})", blob)
                if m:
                    address = re.sub(r"\s+", " ", m.group(1)).strip(" ,|-")

            images = []
            if card:
                for img in card.select("img"):
                    for attr in ("src", "data-src", "srcset"):
                        nu = normalize_image_url(img.get(attr), self.base_url)
                        if nu and "logo" not in nu.lower():
                            # unwrap rentola proxy to original if possible
                            images.append(nu)
                            break
                    if images:
                        break

            external_id = None
            m = re.search(r"-p([a-z0-9]+)$", url, re.I)
            if m:
                external_id = m.group(1)

            out.append(
                {
                    "source": self.source,
                    "external_id": external_id,
                    "url": url,
                    "title": title[:200],
                    "description": blob[:500],
                    "price": price,
                    "rooms": rooms,
                    "size_sqm": size,
                    "address": address if "Augsburg" in address else f"{address}, Augsburg",
                    "district": None,
                    "city": "Augsburg",
                    "image_urls": images[:12],
                    "amenities": [],
                    "status": "active",
                }
            )
            if len(out) >= 40:
                break
        return out

    def _enrich_detail(self, item: dict) -> Optional[dict]:
        url = item["url"]
        resp = self.fetch(url)
        if resp.status_code != 200:
            return item
        soup = BeautifulSoup(resp.text, "lxml")
        out = dict(item)

        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or "")
            except Exception:  # noqa: BLE001
                continue
            if isinstance(data, list):
                candidates = data
            else:
                candidates = [data]
            for data in candidates:
                if not isinstance(data, dict):
                    continue
                if data.get("@type") not in ("RealEstateListing", "Apartment", "Product", "Residence", "SingleFamilyResidence"):
                    # sometimes nested
                    if "offers" not in data and "name" not in data:
                        continue
                name = data.get("name") or data.get("headline")
                if name:
                    out["title"] = str(name)[:200]
                desc = data.get("description")
                if desc:
                    out["description"] = str(desc)[:2000]
                addr = data.get("address")
                if isinstance(addr, dict):
                    parts = [
                        addr.get("streetAddress"),
                        addr.get("postalCode") or addr.get("postalcode"),
                        addr.get("addressLocality") or "Augsburg",
                    ]
                    out["address"] = ", ".join(p for p in parts if p)
                    out["district"] = addr.get("addressRegion") or out.get("district")
                geo = data.get("geo") or {}
                if isinstance(geo, dict):
                    try:
                        if geo.get("latitude") is not None:
                            out["lat"] = float(geo["latitude"])
                        if geo.get("longitude") is not None:
                            out["lon"] = float(geo["longitude"])
                    except (TypeError, ValueError):
                        pass
                # floor size
                fs = data.get("floorSize") or data.get("size")
                if isinstance(fs, dict) and fs.get("value") is not None:
                    out["size_sqm"] = parse_float(str(fs["value"]))
                elif fs is not None and not isinstance(fs, dict):
                    out["size_sqm"] = parse_float(str(fs))
                # rooms / bedrooms
                for key in ("numberOfRooms", "numberOfBedrooms", "rooms"):
                    if data.get(key) is not None:
                        out["rooms"] = parse_float(str(data[key]))
                        break
                offers = data.get("offers")
                if isinstance(offers, dict):
                    price = parse_price(str(offers.get("price") or ""))
                    if price:
                        out["price"] = price
                images = data.get("image") or data.get("photo")
                imgs = []
                if isinstance(images, str):
                    imgs = [images]
                elif isinstance(images, list):
                    for im in images:
                        if isinstance(im, str):
                            imgs.append(im)
                        elif isinstance(im, dict) and im.get("url"):
                            imgs.append(im["url"])
                if imgs:
                    out["image_urls"] = [normalize_image_url(u, self.base_url) or u for u in imgs[:20]]
                    out["image_urls"] = [u for u in out["image_urls"] if u]
                    out["_imgs_from_ld"] = True

        # Fallback parse from visible text if JSON-LD incomplete
        text = soup.get_text("\n", strip=True)
        if not out.get("price"):
            m = re.search(r"(\d+[.,]?\d*)\s*€\s*(?:/|pro)?\s*(?:Monat|Month)?", text, re.I)
            if m:
                out["price"] = parse_price(m.group(1))
        if not out.get("size_sqm"):
            m = re.search(r"(\d+[.,]?\d*)\s*m²", text)
            if m:
                out["size_sqm"] = parse_float(m.group(1))
        # JSON-LD gallery is authoritative — page-wide <img> scans pick up
        # photos from "similar listings" cards, so only fall back to the page
        # when JSON-LD gave us nothing, and even then skip related sections
        # and images linking to other listings.
        if out.pop("_imgs_from_ld", False):
            pass
        elif not out.get("image_urls"):
            detail_imgs = extract_gallery_images(
                soup,
                self.base_url,
                own_url=url,
                listing_link_fragment="/listings/",
                limit=20,
            )
            if detail_imgs:
                out["image_urls"] = detail_imgs

        # Skip shared rooms
        title = (out.get("title") or "").lower()
        if re.search(r"\bwg[-\s]?zimmer\b", title) and "wohnung" not in title:
            return None
        return out
