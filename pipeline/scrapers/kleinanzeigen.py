"""Kleinanzeigen.de apartment rentals scraper (Augsburg)."""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from pipeline.scrapers.base import BaseScraper, abs_url, extract_images_from_soup, normalize_image_url, parse_float, parse_price

logger = logging.getLogger(__name__)

# Wohnung mieten Augsburg (location id l7518 = Augsburg city; l6148 was Schönau!),
# price up to ~900; page 1 has no /seite:N/ segment
SEARCH_URLS = [
    "https://www.kleinanzeigen.de/s-wohnung-mieten/augsburg/preis::900/c203l7518",
] + [
    f"https://www.kleinanzeigen.de/s-wohnung-mieten/augsburg/seite:{n}/preis::900/c203l7518"
    for n in range(2, 6)
]
MAX_LISTINGS = 150
DETAIL_ENRICH_LIMIT = 35  # fetch detail pages for full galleries on this many

# Ad photos: https://img.kleinanzeigen.de/api/v1/prod-ads/images/<id>?rule=$_2.JPG
_IMG_RE = re.compile(
    r"https://img\.kleinanzeigen\.de/api/v1/prod-ads/images/[^\"'\\\s<>?]+",
    re.I,
)


class KleinanzeigenScraper(BaseScraper):
    source = "kleinanzeigen"
    base_url = "https://www.kleinanzeigen.de"

    def scrape(self) -> list[dict]:
        listings: list[dict] = []
        seen = set()
        for page_url in SEARCH_URLS:
            try:
                soup = self.soup(page_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Kleinanzeigen page failed (%s): %s", page_url, exc)
                break

            articles = soup.select("article.aditem, li.ad-listitem article, article[data-adid]")
            if not articles:
                articles = soup.select("[data-adid]")
            if not articles:
                break  # no more result pages

            new_on_page = 0
            for art in articles:
                try:
                    item = self._parse_card(art)
                    if item and item["url"] not in seen:
                        seen.add(item["url"])
                        listings.append(item)
                        new_on_page += 1
                except Exception as exc:  # noqa: BLE001
                    logger.debug("card parse error: %s", exc)
            if new_on_page == 0 or len(listings) >= MAX_LISTINGS:
                break
        listings = listings[:MAX_LISTINGS]
        for item in listings[:DETAIL_ENRICH_LIMIT]:
            try:
                self._enrich_detail(item)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Kleinanzeigen detail failed %s: %s", item.get("url"), exc)
        return listings

    def _enrich_detail(self, item: dict) -> None:
        """Fetch the ad page to collect the full photo gallery + full description."""
        resp = self.fetch(item["url"])
        if resp.status_code != 200:
            return
        soup = BeautifulSoup(resp.text, "lxml")

        # Only take photos inside the ad's own gallery — a page-wide scan also
        # captures thumbnails of "Ähnliche Anzeigen" (other people's ads).
        photos: list[str] = []
        for el in soup.select(
            "#viewad-image img, #viewad-product .galleryimage-element img, "
            "#viewad-product [data-imgsrc], .galleryimage-large--container img"
        ):
            for attr in ("data-imgsrc", "data-src", "src", "srcset"):
                v = el.get(attr) or ""
                m = _IMG_RE.search(v)
                if m:
                    full = f"{m.group(0)}?rule=$_59.JPG"  # largest rendering rule
                    if full not in photos:
                        photos.append(full)
                    break
        if photos:
            item["image_urls"] = photos[:15]
        desc = soup.select_one("#viewad-description-text")
        if desc:
            txt = desc.get_text("\n", strip=True)
            if len(txt) > len(item.get("description") or ""):
                item["description"] = txt[:3000]

        # Exact street address if the poster shared it
        addr = soup.select_one("#viewad-locality")
        if addr:
            txt = re.sub(r"\s+", " ", addr.get_text(" ", strip=True)).strip(" ,-")
            if txt and "augsburg" in txt.lower():
                item["address"] = txt

    def _parse_card(self, art) -> Optional[dict]:
        adid = art.get("data-adid") or art.get("data-href", "")
        a = art.select_one("a[href*='/s-anzeige/'], a.ellipsis")
        if not a:
            a = art.select_one("a[href]")
        href = a.get("href") if a else None
        url = abs_url(self.base_url, href)
        if not url or "/s-anzeige/" not in url:
            return None

        title_el = art.select_one("h2, .text-module-begin a, a.ellipsis")
        title = title_el.get_text(strip=True) if title_el else "Wohnung Augsburg"

        # Skip pure WG rooms if title clearly says so
        tl = title.lower()
        if ("wg" in tl or "zimmer" in tl) and "wohnung" not in tl and "apartment" not in tl and "1-zi" not in tl:
            if "mitbewohner" in tl or "wg-zimmer" in tl or re.search(r"\bwg\b", tl):
                return None
        # Skip "wanted" ads (people looking for a flat)
        if any(x in tl for x in ("suche", "gesucht", "looking for", "gesucht wird")):
            return None

        price_el = art.select_one(".aditem-main--middle--price-shipping--price, .price, p.aditem-main--middle--price")
        price = parse_price(price_el.get_text() if price_el else None)
        if price is not None and price < 50:
            return None  # VB / placeholder prices

        desc_el = art.select_one(".aditem-main--middle--description, p.aditem-main--middle--description")
        description = desc_el.get_text(" ", strip=True) if desc_el else ""

        loc_el = art.select_one(".aditem-main--top--left, .aditem-main--middle--location")
        address = loc_el.get_text(" ", strip=True) if loc_el else "Augsburg"
        # Clean date fragments
        address = re.sub(r"\d{2}\.\d{2}\.\d{4}", "", address).strip(" ,·-|")

        # Soft filter: skip obvious far-away towns mis-tagged in Augsburg search
        if re.search(
            r"\b(Bad Neustadt|Niederlauer|Hohenroth|Oberelsbach|Sandberg|Burgwallbach)\b",
            f"{title} {address}",
            re.I,
        ):
            return None

        # Keep Augsburg metro area only
        loc_l = address.lower()
        if "augsburg" not in loc_l and not re.search(
            r"\b(göggingen|goeggingen|pfersee|lechhausen|hochfeld|haunstetten|"
            r"kriegshaber|oberhausen|innenstadt|universität|universitaet)\b",
            loc_l,
        ):
            if not re.search(r"\b861[5-9]\d\b", address):
                return None

        tags = art.select(".aditem-main--middle--tags span, .text-module-end")
        rooms = size = None
        tag_text = " ".join(t.get_text(" ", strip=True) for t in tags)
        blob = f"{title} {description} {tag_text}"
        m = re.search(r"(\d+[.,]?\d*)\s*Zimmer", blob, re.I)
        if m:
            rooms = parse_float(m.group(1))
        m = re.search(r"(\d+[.,]?\d*)\s*m²", blob)
        if m:
            size = parse_float(m.group(1))

        img = art.select_one("img")
        images = []
        if img:
            for attr in ("data-src", "data-imgsrc", "data-srcset", "srcset", "src"):
                nu = normalize_image_url(img.get(attr), self.base_url)
                if nu:
                    images.append(nu)
                    break
        # also try picture sources
        if not images:
            for src in art.select("source[srcset], img"):
                nu = normalize_image_url(src.get("srcset") or src.get("src"), self.base_url)
                if nu:
                    images.append(nu)
                    break

        district = address.split(",")[0].strip() if address else None

        return {
            "source": self.source,
            "external_id": str(adid) if adid else None,
            "url": url.split("?")[0],
            "title": title,
            "description": description,
            "price": price,
            "rooms": rooms,
            "size_sqm": size,
            "address": address if "Augsburg" in address else f"{address}, Augsburg",
            "district": district,
            "city": "Augsburg",
            "image_urls": images,
            "amenities": [],
            "status": "active",
        }
