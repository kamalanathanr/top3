"""
Article images for news tiles.

Many feeds (DEV, Economic Times, Al Jazeera, CNN, Bloomberg, Hacker News…) send
no image, and Google News items point at a redirect rather than the article.
For those, fetch the article once and use its og:image / twitter:image —
the picture the publisher shows when the page is shared. Google News links are
first decoded to the real article URL.
"""

import json
import logging
import re
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("news.images")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 10
MAX_HEAD_BYTES = 600_000       # og tags live in <head>; don't download whole pages

# Placeholder / logo images some sites use for every article — not worth showing
_GENERIC = re.compile(r"(^|[/_-])(default|placeholder|logo|favicon|sprite|blank)([._-]|$)|og-image\.png$|/static/.*share", re.I)


class GoogleRateLimited(Exception):
    """news.google.com answered 429 — stop decoding for a while."""


_META_IMG_TAG = re.compile(rb"<meta[^>]+(?:og:image|twitter:image)[^>]*>", re.I)
_ESCAPED_OG = re.compile(r'og:image\\?"\s*,\s*\\?"content\\?"\s*:\s*\\?"(https?://[^"\\]+)')


def decode_google_news(url: str, session: requests.Session | None = None) -> str | None:
    """news.google.com/rss/articles/<id> → the publisher's article URL (None if it can't be decoded)."""
    m = re.search(r"/(?:rss/)?articles/([^?/]+)", url)
    if "news.google.com" not in url or not m:
        return url
    s = session or requests.Session()
    art_id = m.group(1)
    page = s.get(f"https://news.google.com/articles/{art_id}", headers=HEADERS, timeout=TIMEOUT)
    if page.status_code == 429:
        raise GoogleRateLimited()
    sig = re.search(r'data-n-a-sg="([^"]+)"', page.text)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page.text)
    if not (sig and ts):
        return None
    payload = ["Fbv4je", f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
                         f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{art_id}",{ts.group(1)},"{sig.group(1)}"]']
    resp = s.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                  headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                  data=f"f.req={quote(json.dumps([[payload]]))}", timeout=TIMEOUT)
    if resp.status_code == 429:
        raise GoogleRateLimited()
    body = resp.text.split("\n\n", 1)[-1]
    try:
        inner = json.loads(json.loads(body)[0][2])
        return inner[1] if inner and inner[0] == "garturlres" else None
    except (ValueError, IndexError, TypeError):
        return None


def og_image(url: str, session: requests.Session | None = None) -> str | None:
    """The article's share image (og:image → twitter:image → image_src), or None."""
    s = session or requests.Session()
    with s.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True, allow_redirects=True) as resp:
        if resp.status_code >= 400 or "html" not in resp.headers.get("Content-Type", "html"):
            return None
        # Some sites (e.g. Dinamalar) close <head> early and put og: tags further down —
        # read until we've seen a complete og:image / twitter:image tag, </body>, or the cap
        raw = b""
        for chunk in resp.iter_content(32_768):
            raw += chunk
            if _META_IMG_TAG.search(raw) or b"</body>" in raw.lower() or len(raw) > MAX_HEAD_BYTES:
                break
        base = resp.url
    soup = BeautifulSoup(raw.decode("utf-8", "ignore"), "html.parser")
    for attr, key in (("property", "og:image"), ("property", "og:image:url"), ("name", "twitter:image"),
                      ("property", "twitter:image"), ("name", "twitter:image:src")):
        tag = soup.find("meta", attrs={attr: key})
        if tag and tag.get("content"):
            img = urljoin(base, tag["content"].strip())
            if img.startswith("http") and not _GENERIC.search(urlparse(img).path):
                return img
    link = soup.find("link", rel="image_src")
    if link and link.get("href"):
        return urljoin(base, link["href"])
    # Next.js / streamed pages: the og:image only exists inside escaped script data
    m = _ESCAPED_OG.search(raw.decode("utf-8", "ignore"))
    if m and not _GENERIC.search(urlparse(m.group(1)).path):
        return m.group(1)
    return None


def find_image(link: str) -> tuple[str | None, str | None]:
    """(image, resolved_article_url) for a feed link — decodes Google News first."""
    s = requests.Session()
    article = decode_google_news(link, s) if "news.google.com" in link else link
    if not article:
        return None, None
    return og_image(article, s), article


# ── Backfill for stored articles (run by the Celery news refresh) ────────

GN_PER_RUN = 60                 # Google News decodes per refresh (each is 2 requests)
GN_SPACING_SEC = 1.5            # pause between decodes — Google rate-limits bursts
GN_BACKOFF = 30 * 60            # after a 429, leave Google alone this long
MAX_ATTEMPTS = 3                # transient failures retried this many times


def enrich_images(db, limit: int = 300, time_budget: float = 120.0) -> dict:
    """Find images for image-less articles (sync, pymongo). Direct publisher links are
    fetched in parallel; Google News links are decoded one at a time, paced, and
    paused entirely for GN_BACKOFF after Google returns 429."""
    import time
    from collections import defaultdict
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import datetime, timedelta, timezone
    from threading import BoundedSemaphore

    started = time.monotonic()
    base = {"image": {"$in": [None, ""]}, "image_checked": {"$ne": True}, "muted": {"$ne": True},
            "image_attempts": {"$not": {"$gte": MAX_ATTEMPTS}}}
    found = tried = 0

    def save(doc, img, article, definitive):
        nonlocal found
        update = {}
        if img:
            update["image"] = img
            found += 1
        if article and article != doc["link"]:
            update["article_url"] = article
        if definitive or img:
            update["image_checked"] = True
            db.news_items.update_one({"_id": doc["_id"]}, {"$set": update})
        else:
            db.news_items.update_one({"_id": doc["_id"]}, {"$set": update, "$inc": {"image_attempts": 1}})

    # 1. Publisher links — parallel, at most 3 per site
    direct = list(db.news_items.find({**base, "$or": [{"link": {"$not": re.compile("news\\.google\\.com")}},
                                                      {"article_url": {"$exists": True}}]}, {"link": 1, "article_url": 1})
                  .sort("score", -1).limit(limit))
    hosts = defaultdict(lambda: BoundedSemaphore(3))

    def run(doc):
        if time.monotonic() - started > time_budget:
            return doc, None, None, None
        try:
            url = doc.get("article_url") or doc["link"]
            with hosts[urlparse(url).netloc]:
                return doc, og_image(url), None, True
        except Exception:
            return doc, None, None, False   # timeout / connection — try again later

    with ThreadPoolExecutor(max_workers=12) as pool:
        for fut in as_completed([pool.submit(run, d) for d in direct]):
            doc, img, article, definitive = fut.result()
            if definitive is None:
                continue
            tried += 1
            save(doc, img, article, definitive)

    # 2. Google News links — decode one at a time, paced, unless Google asked us to back off
    gn_note = ""
    backoff = db.news_meta.find_one({"_id": "gn_backoff"}) or {}
    now = datetime.now(timezone.utc)
    if backoff.get("until") and backoff["until"].replace(tzinfo=timezone.utc) > now:
        gn_note = "Google News paused (rate-limited)"
    else:
        gn = list(db.news_items.find({**base, "link": re.compile("news\\.google\\.com"), "article_url": {"$exists": False}}, {"link": 1})
                  .sort("score", -1).limit(GN_PER_RUN))
        session = requests.Session()
        for doc in gn:
            if time.monotonic() - started > time_budget:
                break
            tried += 1
            try:
                article = decode_google_news(doc["link"], session)
                if not article:
                    save(doc, None, None, False)
                else:
                    try:
                        save(doc, og_image(article, session), article, True)
                    except Exception:
                        save(doc, None, article, False)
            except GoogleRateLimited:
                db.news_meta.replace_one({"_id": "gn_backoff"}, {"until": now + timedelta(seconds=GN_BACKOFF)}, upsert=True)
                gn_note = "Google News rate-limited — pausing 30 min"
                log.info("[images] %s", gn_note)
                break
            except Exception:
                save(doc, None, None, False)
            time.sleep(GN_SPACING_SEC)
    return {"tried": tried, "found": found, "note": gn_note}


# ── Image relay for publishers that block hot-linking (e.g. Investing.com → 403) ──

MAX_IMAGE_BYTES = 5_000_000
_relay_cache: dict = {}          # url → (content_type, bytes); small in-memory LRU
_relay_failed: dict = {}         # url → when it failed (bot-protected CDNs like Investing.com's) — skip for a day
_RELAY_CACHE_MAX = 300


def _is_public_host(host: str) -> bool:
    import ipaddress
    import socket
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        return True
    except Exception:
        return False


def relay_image(url: str, referer: str | None) -> tuple[str, bytes] | None:
    """Fetch an article image server-side (with the publisher as referrer). Caller must
    only pass URLs that are stored as an article image."""
    import time
    if url in _relay_cache:
        return _relay_cache[url]
    if time.time() - _relay_failed.get(url, 0) < 86_400:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not _is_public_host(parsed.hostname or ""):
        return None
    headers = {**HEADERS, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    if referer:
        headers["Referer"] = referer
    with requests.get(url, headers=headers, timeout=TIMEOUT, stream=True) as resp:
        ctype = resp.headers.get("Content-Type", "")
        if resp.status_code >= 400 or not ctype.startswith("image/"):
            _relay_failed[url] = time.time()
            return None
        data = b""
        for chunk in resp.iter_content(65_536):
            data += chunk
            if len(data) > MAX_IMAGE_BYTES:
                return None
    if len(_relay_cache) >= _RELAY_CACHE_MAX:
        _relay_cache.pop(next(iter(_relay_cache)))
    _relay_cache[url] = (ctype, data)
    return ctype, data
