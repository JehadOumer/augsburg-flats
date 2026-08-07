"""Shared HTTP helpers and scraper base class."""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from pipeline import db
from pipeline.config import PRICE_HARD_MAX, USER_AGENT
from pipeline.scoring import prepare_listing

logger = logging.getLogger(__name__)

_SKIP_IMAGE_FRAGMENTS = (
    "logo",
    "icon",
    "sprite",
    "avatar",
    "placeholder",
    "1x1",
    "pixel",
    "facebook",
    "youtube",
    "instagram",
    "strapi/",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_image_url(raw: str | None, base: str = "") -> Optional[str]:
    """Turn lazy/Next.js/srcset URLs into a direct high-quality absolute image URL."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw or raw.startswith("data:"):
        return None
    # srcset: prefer the largest width candidate
    if "," in raw and (" w" in raw or re.search(r"\s\d+w", raw)):
        best = None
        best_w = -1
        for part in raw.split(","):
            bits = part.strip().split()
            if not bits:
                continue
            w = 0
            if len(bits) > 1 and bits[1].endswith("w"):
                try:
                    w = int(bits[1][:-1])
                except ValueError:
                    w = 0
            if w >= best_w:
                best_w = w
                best = bits[0]
        raw = best or raw.split(",")[0].strip().split(" ")[0]
    if base:
        raw = urljoin(base, raw)
    # Next.js image optimizer: /_next/image/?url=https%3A%2F%2F...
    if "/_next/image" in raw and "url=" in raw:
        try:
            qs = parse_qs(urlparse(raw).query)
            if "url" in qs and qs["url"]:
                raw = unquote(qs["url"][0])
        except Exception:  # noqa: BLE001
            pass
    # Rentola / similar CDN proxies embedding the original after filters
    if "rentola.com" in raw and "http" in raw[10:]:
        m = re.search(r"(https?://(?:image|images|media)[^\"'\s]+)$", unquote(raw))
        if not m:
            # path often ends with /https%3A%2F%2F...
            m = re.search(r"/(https?%3A%2F%2F.+)$", raw, re.I)
            if m:
                raw = unquote(m.group(1))
        else:
            raw = m.group(1)
    # Upscale common size suffixes to a larger variant when possible
    raw = re.sub(r"@\d+x\b", "@2000x", raw)
    raw = re.sub(r"([?&](?:width|w|h|height)=)\d+", r"\g<1>1600", raw, flags=re.I)
    if not raw.startswith("http"):
        return None
    low = raw.lower()
    if any(x in low for x in _SKIP_IMAGE_FRAGMENTS):
        return None
    if low.endswith(".svg"):
        return None
    return raw.split("#")[0]


def extract_images_from_soup(soup: BeautifulSoup, base: str, *, limit: int = 24) -> list[str]:
    """Collect real listing photos from img tags, srcset, og:image, and raw HTML."""
    found: list[str] = []

    def add(u: str | None) -> None:
        nu = normalize_image_url(u, base)
        if nu and nu not in found:
            found.append(nu)

    og = soup.select_one('meta[property="og:image"], meta[name="og:image"], meta[property="twitter:image"]')
    if og:
        add(og.get("content"))

    for img in soup.select("img"):
        for attr in ("data-src", "data-lazy", "data-original", "data-imgsrc", "data-srcset", "srcset", "src"):
            add(img.get(attr))
            if len(found) >= limit:
                return found[:limit]

    # Fallback: direct CDN/media URLs in page source
    html = str(soup)
    for m in re.findall(
        r"https?://[^\s\"'<>]+?\.(?:jpg|jpeg|png|webp)(?:\?[^\s\"'<>]*)?",
        html,
        re.I,
    ):
        add(m.replace("\\u002F", "/").replace("\\/", "/"))
        if len(found) >= limit:
            break

    return found[:limit]


_RELATED_SECTION_RE = re.compile(
    r"similar|related|recommend|weitere|hnliche|andere[-_ ]?angebote|carousel|slider[-_ ]?listings|auch[-_ ]?interessant",
    re.I,
)


def extract_gallery_images(
    soup: BeautifulSoup,
    base: str,
    *,
    own_url: str = "",
    listing_link_fragment: str = "",
    limit: int = 20,
) -> list[str]:
    """Collect photos belonging to THIS listing only.

    Skips images that sit inside "similar/related listings" sections or are
    wrapped in links pointing to a different listing page.
    """
    own = (own_url or "").split("?")[0].rstrip("/")
    found: list[str] = []

    for img in soup.select("img"):
        # Skip images that link out to another listing (related-listing cards)
        a = img.find_parent("a")
        if a and a.get("href") and listing_link_fragment:
            href = abs_url(base, a["href"].split("?")[0]) or ""
            href = href.rstrip("/")
            if listing_link_fragment in href and own and href != own:
                continue

        # Skip images inside related/similar/recommendation sections
        anc = img
        in_related = False
        for _ in range(10):
            anc = anc.parent
            if anc is None or anc.name in ("body", "html"):
                break
            attrs = " ".join(
                filter(
                    None,
                    [" ".join(anc.get("class") or []), anc.get("id") or "", anc.get("data-testid") or ""],
                )
            )
            if attrs and _RELATED_SECTION_RE.search(attrs):
                in_related = True
                break
        if in_related:
            continue

        for attr in ("data-src", "data-lazy", "data-original", "data-imgsrc", "data-srcset", "srcset", "src"):
            nu = normalize_image_url(img.get(attr), base)
            if nu and nu not in found:
                found.append(nu)
                break
        if len(found) >= limit:
            break

    return found[:limit]


def parse_price(text: str | None) -> Optional[float]:
    if not text:
        return None
    # Keep digits, dots, commas
    cleaned = re.sub(r"[^\d,\.]", "", str(text))
    if not cleaned:
        return None
    # German format: 1.200,50 or 660,-
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts[-1]) <= 2:
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    else:
        # 1.200 style thousands
        if cleaned.count(".") == 1 and len(cleaned.split(".")[-1]) == 3:
            cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_float(text: str | None) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"(\d+[.,]?\d*)", str(text).replace(" ", ""))
    if not m:
        return None
    return parse_price(m.group(1))


def parse_rooms(text: str | None) -> Optional[float]:
    return parse_float(text)


def abs_url(base: str, href: str | None) -> Optional[str]:
    if not href:
        return None
    return urljoin(base, href)


class BaseScraper(ABC):
    source: str = "base"
    base_url: str = ""

    def __init__(self, client: Optional[httpx.Client] = None):
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=30.0,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def fetch(self, url: str, **kwargs) -> httpx.Response:
        time.sleep(0.6)  # be polite
        resp = self.client.get(url, **kwargs)
        return resp

    def soup(self, url: str) -> BeautifulSoup:
        resp = self.fetch(url)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")

    @abstractmethod
    def scrape(self) -> list[dict]:
        """Return raw listing dicts (before scoring/upsert)."""

    def run(self, *, do_geocode: bool = True, mark_gone: bool = True) -> dict[str, Any]:
        started = utc_now()
        found = 0
        new_count = 0
        seen_urls: set[str] = set()
        errors: list[str] = []
        try:
            raw_listings = self.scrape()
            for raw in raw_listings:
                url = raw.get("url")
                if not url:
                    continue
                # Soft filter on price if known
                price = raw.get("price")
                if price is not None and price > PRICE_HARD_MAX + 150:
                    # keep a little slack for cold rent + NK ambiguity, but skip extremes
                    continue
                raw["source"] = self.source
                try:
                    prepared = prepare_listing(raw, do_geocode=do_geocode)
                    _, is_new = db.upsert_listing(prepared)
                    seen_urls.add(url)
                    found += 1
                    if is_new:
                        new_count += 1
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Failed to upsert %s", url)
                    errors.append(f"{url}: {exc}")

            gone = 0
            if mark_gone and seen_urls:
                gone = db.mark_missing_as_gone(self.source, seen_urls)

            msg = f"found={found} new={new_count} gone={gone}"
            if errors:
                msg += f" errors={len(errors)}"
            db.log_scrape(
                self.source,
                "ok" if not errors else "partial",
                listings_found=found,
                listings_new=new_count,
                message=msg,
                started_at=started,
            )
            return {
                "source": self.source,
                "status": "ok" if not errors else "partial",
                "found": found,
                "new": new_count,
                "gone": gone,
                "errors": errors,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("Scraper %s failed", self.source)
            db.log_scrape(
                self.source,
                "error",
                listings_found=found,
                listings_new=new_count,
                message=str(exc),
                started_at=started,
            )
            return {
                "source": self.source,
                "status": "error",
                "found": found,
                "new": new_count,
                "gone": 0,
                "errors": [str(exc)],
            }
        finally:
            self.close()
