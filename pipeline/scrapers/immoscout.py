"""ImmobilienScout24 scraper (best-effort).

IS24 often returns 401/captcha for automated clients. We try several
endpoints and header profiles; on failure the UI still shows a manual
search link from config.FALLBACK_SEARCH_LINKS.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from pipeline.config import FALLBACK_SEARCH_LINKS, PRICE_HARD_MAX, USER_AGENT
from pipeline.scrapers.base import BaseScraper, abs_url, normalize_image_url, parse_float, parse_price

logger = logging.getLogger(__name__)

SEARCH_URLS = [
    (
        "https://www.immobilienscout24.de/Suche/de/bayern/augsburg/wohnung-mieten"
        "?price=-900.0&numberofrooms=1.0-&livingspace=15.0-&sorting=2"
    ),
    (
        "https://www.immobilienscout24.de/Suche/de/bayern/augsburg/wohnung-mieten"
        "?enteredFrom=one_step_search&price=-900.0"
    ),
]


class ImmoScout24Scraper(BaseScraper):
    source = "immobilienscout24"
    base_url = "https://www.immobilienscout24.de"

    def __init__(self, client=None):
        super().__init__(client=client)
        # Stronger browser-like headers
        self.client.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Cache-Control": "max-age=0",
            }
        )

    def scrape(self) -> list[dict]:
        listings: list[dict] = []
        last_error = None
        for search_url in SEARCH_URLS:
            try:
                resp = self.fetch(search_url)
                body = resp.text or ""
                if resp.status_code in (401, 403, 429) or "captcha" in body.lower() or "zugriff verweigert" in body.lower():
                    last_error = f"blocked status={resp.status_code}"
                    logger.warning(
                        "ImmoScout24 blocked (%s). Open manually: %s",
                        last_error,
                        FALLBACK_SEARCH_LINKS.get("immobilienscout24"),
                    )
                    continue
                resp.raise_for_status()
                soup = BeautifulSoup(body, "lxml")
                listings = self._parse_page(soup, body)
                if listings:
                    return listings[:40]
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                logger.warning("ImmoScout24 request failed: %s", exc)

        # Alternate: result list JSON sometimes exposed after cookie wall
        try:
            listings = self._try_json_endpoints()
            if listings:
                return listings[:40]
        except Exception as exc:  # noqa: BLE001
            logger.debug("IS24 JSON endpoints failed: %s", exc)

        if not listings:
            logger.warning(
                "ImmoScout24 returned no listings (%s). Fallback: %s",
                last_error,
                FALLBACK_SEARCH_LINKS.get("immobilienscout24"),
            )
        return listings

    def _try_json_endpoints(self) -> list[dict]:
        endpoints = [
            (
                "https://www.immobilienscout24.de/Suche/controller/oneStepSearch/results.json"
                "?realEstateType=apartmentrent&locationName=Augsburg&priceMax=900&pageSize=30"
            ),
        ]
        out: list[dict] = []
        for url in endpoints:
            resp = self.client.get(url, headers={"Accept": "application/json, text/plain, */*"})
            if resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                continue
            out.extend(self._walk_json_hits(data))
        return out

    def _walk_json_hits(self, obj, acc: Optional[list] = None) -> list[dict]:
        if acc is None:
            acc = []
        if isinstance(obj, dict):
            # expose-like object
            if ("title" in obj or "headline" in obj) and (
                "price" in obj or "coldRent" in obj or "warmRent" in obj or "calculatedPrice" in obj
            ):
                parsed = self._from_loose_item(obj)
                if parsed:
                    acc.append(parsed)
            for v in obj.values():
                self._walk_json_hits(v, acc)
        elif isinstance(obj, list):
            for v in obj:
                self._walk_json_hits(v, acc)
        return acc

    def _parse_page(self, soup: BeautifulSoup, body: str) -> list[dict]:
        listings: list[dict] = []
        for script in soup.select("script"):
            text = script.string or ""
            if "resultListModel" in text or "searchResponseModel" in text or "resultlistResultList" in text:
                listings.extend(self._parse_embedded_json(text))
                if listings:
                    return listings

        # __INITIAL_STATE__ style
        m = re.search(r"__INITIAL_STATE__\s*=\s*(\{.+?\})\s*;\s*</script>", body, re.S)
        if m:
            try:
                data = json.loads(m.group(1))
                listings.extend(self._walk_json_hits(data))
                if listings:
                    return listings
            except Exception:  # noqa: BLE001
                pass

        seen = set()
        for a in soup.select('a[href*="/expose/"]'):
            href = a.get("href", "")
            url = abs_url(self.base_url, href)
            if not url or not re.search(r"/expose/\d+", url):
                continue
            url = url.split("?")[0]
            if url in seen:
                continue
            seen.add(url)
            card = a.find_parent(["li", "article", "div"])
            blob = card.get_text(" ", strip=True) if card else a.get_text(" ", strip=True)
            title = a.get("aria-label") or a.get_text(" ", strip=True) or "Wohnung Augsburg"
            if card:
                h = card.select_one("h2, h5, [data-testid='result-list-entry__brand-title-container']")
                if h:
                    title = h.get_text(" ", strip=True)
            price = None
            m = re.search(r"(\d+[.,]?\d*)\s*€", blob)
            if m:
                price = parse_price(m.group(1))
            if price is not None and price > PRICE_HARD_MAX + 200:
                continue
            rooms = size = None
            m = re.search(r"(\d+[.,]?\d*)\s*(?:Zi\.|Zimmer)", blob, re.I)
            if m:
                rooms = parse_float(m.group(1))
            m = re.search(r"(\d+[.,]?\d*)\s*m²", blob)
            if m:
                size = parse_float(m.group(1))
            images = []
            if card:
                img = card.select_one("img")
                if img:
                    nu = normalize_image_url(img.get("src") or img.get("data-src"), self.base_url)
                    if nu:
                        images.append(nu)
            listings.append(
                {
                    "source": self.source,
                    "external_id": re.search(r"/expose/(\d+)", url).group(1),
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
            if len(listings) >= 40:
                break
        return listings

    def _parse_embedded_json(self, text: str) -> list[dict]:
        out = []
        m = re.search(r'"resultListModel"\s*:\s*(\{.+?\})\s*,\s*"', text, re.S)
        if not m:
            m = re.search(r"resultListModel\s*=\s*(\{.+?\});\s*", text, re.S)
        if not m:
            return out
        try:
            data = json.loads(m.group(1))
        except Exception:  # noqa: BLE001
            return out
        hits = (
            data.get("searchResponseModel", {})
            .get("resultlist.resultlist", {})
            .get("resultlistEntries", [])
        )
        entries = []
        if isinstance(hits, list):
            for h in hits:
                if isinstance(h, dict) and "resultlistEntry" in h:
                    e = h["resultlistEntry"]
                    if isinstance(e, list):
                        entries.extend(e)
                    else:
                        entries.append(e)
        for e in entries:
            parsed = self._from_entry(e)
            if parsed:
                out.append(parsed)
        return out

    def _from_loose_item(self, it: dict) -> Optional[dict]:
        oid = it.get("@id") or it.get("id") or it.get("exposeId")
        url = it.get("url") or it.get("detailUrl")
        if not url and oid:
            url = f"https://www.immobilienscout24.de/expose/{oid}"
        if not url:
            return None
        url = abs_url(self.base_url, str(url)).split("?")[0]
        title = it.get("title") or it.get("headline") or "Wohnung Augsburg"
        price = None
        for key in ("price", "warmRent", "coldRent", "calculatedPrice", "priceValue"):
            val = it.get(key)
            if isinstance(val, dict):
                price = parse_price(str(val.get("value") or val.get("amount") or ""))
            elif val is not None:
                price = parse_price(str(val))
            if price:
                break
        rooms = parse_float(str(it.get("numberOfRooms") or it.get("rooms") or ""))
        size = parse_float(str(it.get("livingSpace") or it.get("size") or ""))
        addr = it.get("address") or {}
        address = "Augsburg"
        if isinstance(addr, dict):
            address = ", ".join(
                filter(
                    None,
                    [addr.get("street"), addr.get("quarter"), addr.get("postcode"), addr.get("city") or "Augsburg"],
                )
            ) or "Augsburg"
        return {
            "source": self.source,
            "external_id": str(oid) if oid else None,
            "url": url,
            "title": str(title)[:200],
            "description": str(it.get("description") or "")[:1500],
            "price": price,
            "rooms": rooms,
            "size_sqm": size,
            "address": address,
            "district": addr.get("quarter") if isinstance(addr, dict) else None,
            "city": "Augsburg",
            "image_urls": [],
            "status": "active",
        }

    def _from_entry(self, e: dict) -> Optional[dict]:
        if not isinstance(e, dict):
            return None
        real = e.get("resultlist.realEstate") or e.get("realEstate") or e
        if not isinstance(real, dict):
            real = e if isinstance(e, dict) else {}
        return self._from_loose_item(real)
