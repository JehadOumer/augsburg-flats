"""HC24 Augsburg furnished apartments scraper."""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from pipeline.config import HC24_SEED_URLS
from pipeline.scrapers.base import BaseScraper, abs_url, extract_images_from_soup, parse_float, parse_price

logger = logging.getLogger(__name__)

SEARCH_URLS = [
    "https://www.hc24.de/de/wohnungen/augsburg/",
    "https://www.hc24.de/en/apartments/augsburg/",
]


class HC24Scraper(BaseScraper):
    source = "hc24"
    base_url = "https://www.hc24.de"

    def scrape(self) -> list[dict]:
        urls = set(HC24_SEED_URLS)
        for search in SEARCH_URLS:
            try:
                soup = self.soup(search)
                for a in soup.select("a[href*='/expose/'], a[href*='/de/expose/'], a[href*='/en/expose/']"):
                    href = a.get("href")
                    full = abs_url(self.base_url, href)
                    if full and "/expose/" in full:
                        # normalize trailing slash
                        if not full.endswith("/"):
                            full += "/"
                        urls.add(full.split("?")[0])
            except Exception as exc:  # noqa: BLE001
                logger.warning("HC24 search failed %s: %s", search, exc)

        listings = []
        for url in sorted(urls):
            try:
                item = self.parse_expose(url)
                if item:
                    listings.append(item)
            except Exception as exc:  # noqa: BLE001
                logger.warning("HC24 expose failed %s: %s", url, exc)
        return listings

    def parse_expose(self, url: str) -> Optional[dict]:
        resp = self.fetch(url)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        title_el = soup.select_one("h1")
        title = title_el.get_text(strip=True) if title_el else "HC24 Apartment"

        # Street / district
        address = None
        for sel in (".expose-address", ".address", "h1 + p", ".location"):
            el = soup.select_one(sel)
            if el:
                address = el.get_text(" ", strip=True)
                break
        # Max-Reger-Straße pattern from page text
        text = soup.get_text("\n", strip=True)
        if not address:
            m = re.search(r"(Max-Reger-Straße|[A-ZÄÖÜ][a-zäöüß\-]+(?:straße|strasse|weg|platz|allee))", text)
            if m:
                address = f"{m.group(1)}, Augsburg"

        # Object number
        external_id = None
        m = re.search(r"(?:Objektnummer|Objectnumber|Objektnr\.?)\s*[:\s]*([A-Za-z0-9\-]+)", text, re.I)
        if m:
            external_id = m.group(1)
        else:
            m = re.search(r"/expose/(au\d+)", url, re.I)
            if m:
                external_id = m.group(1).upper()

        price = None
        # DE uses . as thousands sep: "1.420 €/ Monat". Prefer full amounts first.
        for pat in (
            r"(?:Miete|Rent)\s*[:\s]*€?\s*(\d{1,3}(?:\.\d{3})+|\d{1,3}(?:,\d{3})+|\d+)\s*€?",
            r"(\d{1,3}(?:\.\d{3})+)\s*€\s*(?:/|pro)?\s*(?:Monat|Month)?",
            r"€\s*(\d{1,3}(?:,\d{3})+|\d{1,3}(?:\.\d{3})+|\d+)\s*(?:/|per)?\s*(?:Monat|Month)",
            r"(\d{3,4})\s*€\s*(?:/|pro)?\s*(?:Monat|Month|pro Monat)",
            r"(\d{3,4})\s*€\s*pro\s*Monat",
        ):
            m = re.search(pat, text, re.I)
            if m:
                price = parse_price(m.group(1))
                if price and 150 <= price <= 8000:
                    break
                price = None
        if price is None:
            m = re.search(
                r"(?:^|\n)\s*€?\s*(\d{1,3}(?:[.,]\d{3})+|\d{3,4})\s*€?\s*/\s*(?:Monat|Month)",
                text,
                re.I,
            )
            if m:
                price = parse_price(m.group(1))


        rooms = None
        m = re.search(r"(?:Zimmer|Rooms)\s*(\d+[.,]?\d*)", text, re.I)
        if m:
            rooms = parse_float(m.group(1))

        size = None
        m = re.search(r"(?:Größe|Size)\s*(\d+[.,]?\d*)\s*m", text, re.I)
        if m:
            size = parse_float(m.group(1))
        if size is None:
            m = re.search(r"(\d+[.,]?\d*)\s*m²", text)
            if m:
                size = parse_float(m.group(1))

        floor = None
        m = re.search(r"(?:Etage|Floor)\s*(\d+|EG|DG|0)", text, re.I)
        if m:
            floor = m.group(1)

        deposit = None
        m = re.search(r"(?:Kaution|Deposit)\s*(\d+[.,]?\d*)\s*€", text, re.I)
        if m:
            deposit = parse_price(m.group(1))

        available_from = None
        rented = False
        if re.search(r"vermietet|already rented|schon seit", text, re.I):
            rented = True
            m = re.search(r"(?:Vermietet seit|rented since)\s*(\d{2}\.\d{2}\.\d{4})", text, re.I)
            if m:
                available_from = _de_date(m.group(1))
        else:
            m = re.search(
                r"(?:Frei ab|Free from|available from)\s*(\d{2}[./]\d{2}[./]\d{4})",
                text,
                re.I,
            )
            if m:
                available_from = _de_date(m.group(1))

        max_persons = None
        m = re.search(r"(?:Max\.?\s*)?(\d+)\s*(?:Person|Personen)", text, re.I)
        if m:
            max_persons = int(m.group(1))

        rental_duration = None
        m = re.search(r"(mindestens\s*\d+\s*Tage|at least\s*\d+\s*days|maximal\s*\d+\s*Monate)", text, re.I)
        if m:
            rental_duration = m.group(1)

        # Description: first meaningful paragraph under Beschreibung / Description
        description = ""
        for heading in soup.find_all(["h2", "h3"]):
            ht = heading.get_text(strip=True).lower()
            if "beschreibung" in ht or "description" in ht:
                parts = []
                for sib in heading.find_all_next(["p", "div"]):
                    if sib.name in ("h2", "h3"):
                        break
                    t = sib.get_text(" ", strip=True)
                    if len(t) > 80:
                        parts.append(t)
                        break
                if parts:
                    description = parts[0]
                break
        if not description:
            # fallback: longest paragraph
            paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
            paras = [p for p in paras if len(p) > 100]
            if paras:
                description = max(paras, key=len)[:2000]

        amenities = []
        for li in soup.select("ul li"):
            t = li.get_text(" ", strip=True)
            if 2 < len(t) < 60:
                amenities.append(t)
        amenities = list(dict.fromkeys(amenities))[:40]

        # Prefer object photos from S3 (hcau/<id>_n.jpg); skip staff avatars etc.
        images = extract_images_from_soup(soup, self.base_url, limit=30)
        obj_num = None
        m = re.search(r"/expose/au(\d+)", url, re.I)
        if m:
            obj_num = m.group(1)
        if obj_num:
            matched = [u for u in images if re.search(rf"/hcau/{re.escape(obj_num)}_\d+\.", u, re.I)]
            if matched:
                images = matched
            else:
                images = [
                    u
                    for u in images
                    if "hc24-media" in u.lower() and "/strapi/" not in u.lower()
                ]
        else:
            images = [
                u
                for u in images
                if "hc24-media" in u.lower() or "/hcau/" in u.lower()
            ] or images
        images = images[:20]

        district = None
        if "göggingen" in text.lower() or "goeggingen" in text.lower():
            district = "Göggingen"
        elif "pfersee" in text.lower():
            district = "Pfersee"
        elif address:
            district = address

        # Approx coords for Max-Reger-Straße / Göggingen
        lat = lon = None
        if district == "Göggingen" or (address and "Max-Reger" in (address or "")):
            lat, lon = 48.3445, 10.8705

        return {
            "source": self.source,
            "external_id": external_id,
            "url": url if url.endswith("/") else url + "/",
            "title": title,
            "description": description,
            "price": price,
            "rooms": rooms or 1,
            "size_sqm": size,
            "floor": floor,
            "address": address or ("Max-Reger-Straße, Augsburg" if district == "Göggingen" else "Augsburg"),
            "district": district,
            "city": "Augsburg",
            "lat": lat,
            "lon": lon,
            "available_from": available_from,
            "furnished": True,
            "deposit": deposit,
            "max_persons": max_persons,
            "rental_duration": rental_duration,
            "image_urls": images,
            "amenities": amenities,
            "status": "gone" if rented else "active",
        }


def _de_date(s: str) -> str:
    s = s.replace("/", ".")
    parts = s.split(".")
    if len(parts) == 3:
        d, m, y = parts
        return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    return s
