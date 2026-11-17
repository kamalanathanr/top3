#!/usr/bin/env python3
"""Scraper for indiabonds.com — refactored for Top3 platform with MongoDB storage."""

import json
import random
import re
import time
from datetime import datetime
from xml.etree import ElementTree as ET

import requests
import pymongo  # sync driver — scraper runs in Celery worker process

BASE_URL        = "https://www.indiabonds.com"
SITEMAP_URL     = f"{BASE_URL}/bond-details.xml"
BOND_PLATFORM   = "indiabonds"
MONGO_URL       = "mongodb://admin:admin@localhost:27017/"
DB_NAME         = "top3"

REQUEST_DELAY  = 2.0
MAX_RETRIES    = 6
BACKOFF_BASE   = 2
BACKOFF_MAX    = 120
BACKOFF_JITTER = 1.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

_STEALTH_JS = """
(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    if (!window.chrome) {
        window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {}, app: {} };
    }
    Object.defineProperty(navigator, 'plugins', {
        get: () => Object.assign([1, 2, 3], { item: () => null, namedItem: () => null, refresh: () => {} })
    });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    const _origQuery = window.navigator.permissions.query.bind(navigator.permissions);
    window.navigator.permissions.query = (p) =>
        p.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : _origQuery(p);
})();
"""

_PW_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--window-size=1920,1080",
]

_PW_CONTEXT_OPTS = dict(
    viewport={"width": 1920, "height": 1080},
    user_agent=(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    locale="en-US",
    timezone_id="Asia/Kolkata",
    extra_http_headers={
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    },
)


# ─── MongoDB Scrape Logger ────────────────────────────────────────────────────

class MongoScrapeLogger:
    """Writes scrape progress to bonds_scrape_log collection in real-time."""

    def __init__(self, db, task_id: str = ""):
        self.col = db["bonds_scrape_log"]
        self.task_id = task_id
        self._run: dict = {}
        self._bonds: dict[str, dict] = {}

    def _ts(self) -> str:
        return datetime.now().isoformat(timespec="milliseconds")

    def _evt(self, **kwargs) -> dict:
        return {"ts": self._ts(), **kwargs}

    def _flush(self) -> None:
        self.col.replace_one(
            {"_id": "current"},
            {"_id": "current", "run": self._run, "bonds": self._bonds},
            upsert=True,
        )

    def run_start(self, total_urls: int) -> None:
        self._run = {
            "task_id": self.task_id,
            "started_at": self._ts(),
            "total_urls": total_urls,
            "is_running": True,
            "completed_at": None,
        }
        self._flush()

    def run_end(self) -> None:
        self._run["is_running"] = False
        self._run["completed_at"] = self._ts()
        self._flush()

    def bond_start(self, isin: str, url: str) -> None:
        self._bonds[isin] = {
            "isin": isin,
            "url": url,
            "status": "fetching",
            "attempts": 0,
            "issuer_name": None,
            "ytm": None,
            "rating": None,
            "coupon_rate": None,
            "started_at": self._ts(),
            "completed_at": None,
            "duration_ms": None,
            "events": [self._evt(type="start", msg="Starting scrape")],
        }
        self._flush()

    def log_attempt(self, isin: str, attempt: int) -> None:
        b = self._bonds[isin]
        b["attempts"] = attempt
        b["events"].append(self._evt(type="fetch_start", attempt=attempt,
                                     msg=f"Attempt {attempt} — sending request…"))
        self._flush()

    def log_http(self, isin: str, attempt: int,
                 http_status: int, size_bytes: int, time_ms: float) -> None:
        b = self._bonds[isin]
        b["events"].append(self._evt(
            type="http_response", attempt=attempt, http_status=http_status,
            size_kb=round(size_bytes / 1024, 1), time_ms=round(time_ms),
            msg=f"HTTP {http_status} · {size_bytes/1024:.1f} KB · {time_ms:.0f} ms",
        ))
        self._flush()

    def log_empty(self, isin: str, attempt: int, wait_s: float) -> None:
        b = self._bonds[isin]
        b["status"] = "empty_data"
        b["events"].append(self._evt(type="empty_data", attempt=attempt,
                                     backoff_s=round(wait_s, 1),
                                     msg=f"Empty data — backing off {wait_s:.0f}s"))
        self._flush()

    def log_rate_limit(self, isin: str, attempt: int,
                       http_status: int, wait_s: float) -> None:
        b = self._bonds[isin]
        b["status"] = "rate_limited"
        b["events"].append(self._evt(type="rate_limited", attempt=attempt,
                                     http_status=http_status, backoff_s=round(wait_s, 1),
                                     msg=f"HTTP {http_status} rate-limited — backing off {wait_s:.0f}s"))
        self._flush()

    def log_network_error(self, isin: str, attempt: int,
                          error: str, wait_s: float) -> None:
        b = self._bonds[isin]
        b["status"] = "network_error"
        b["events"].append(self._evt(type="network_error", attempt=attempt,
                                     backoff_s=round(wait_s, 1), error=error,
                                     msg=f"Network error: {error} — backing off {wait_s:.0f}s"))
        self._flush()

    def log_success(self, isin: str, bond_data: dict, started_mono: float) -> None:
        b = self._bonds[isin]
        b["status"] = "success"
        b["issuer_name"] = bond_data.get("issuer_name")
        b["ytm"] = bond_data.get("ytm")
        b["rating"] = bond_data.get("rating")
        b["coupon_rate"] = bond_data.get("coupon_rate")
        b["completed_at"] = self._ts()
        b["duration_ms"] = round((time.monotonic() - started_mono) * 1000)
        b["events"].append(self._evt(
            type="success",
            issuer=bond_data.get("issuer_name"),
            ytm=bond_data.get("ytm"),
            rating=bond_data.get("rating"),
            msg=(f"✓ {bond_data.get('issuer_name', '?')} | "
                 f"YTM={bond_data.get('ytm', '?')} | Rating={bond_data.get('rating', '?')}"),
        ))
        self._flush()

    def log_incomplete(self, isin: str, started_mono: float) -> None:
        b = self._bonds[isin]
        b["status"] = "incomplete"
        b["completed_at"] = self._ts()
        b["duration_ms"] = round((time.monotonic() - started_mono) * 1000)
        b["events"].append(self._evt(type="incomplete",
                                     msg=f"Gave up after {MAX_RETRIES} attempts"))
        self._flush()

    def log_failure(self, isin: str, error: str, started_mono: float) -> None:
        b = self._bonds[isin]
        b["status"] = "failed"
        b["completed_at"] = self._ts()
        b["duration_ms"] = round((time.monotonic() - started_mono) * 1000)
        b["events"].append(self._evt(type="error", error=error, msg=f"Failed: {error}"))
        self._flush()

    def log_skip(self, isin: str, url: str) -> None:
        if isin not in self._bonds:
            self._bonds[isin] = {
                "isin": isin, "url": url, "status": "skipped", "attempts": 0,
                "issuer_name": None, "ytm": None, "rating": None, "coupon_rate": None,
                "started_at": self._ts(), "completed_at": self._ts(), "duration_ms": 0,
                "events": [self._evt(type="skipped", msg="Already scraped — skipping")],
            }
        self._flush()

    def load_from_db(self) -> None:
        """Restore prior bond state from MongoDB (for resume)."""
        try:
            doc = self.col.find_one({"_id": "current"})
            if doc:
                self._run = doc.get("run", {})
                raw_bonds = doc.get("bonds", {})
                if isinstance(raw_bonds, dict):
                    self._bonds = raw_bonds
                elif isinstance(raw_bonds, list):
                    self._bonds = {b["isin"]: b for b in raw_bonds if "isin" in b}
        except Exception:
            pass


# ─── URL Discovery ────────────────────────────────────────────────────────────

def get_bond_urls() -> list[str]:
    session = requests.Session()
    session.headers.update(HEADERS)
    r = session.get(SITEMAP_URL, timeout=30)
    r.raise_for_status()
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(r.content)
    sitemap_urls = [u.find("sm:loc", ns).text for u in root.findall("sm:url", ns)]

    isins = []
    for url in sitemap_urls:
        m = re.search(r'/bond-details/([A-Z0-9]+)/', url)
        if m:
            isins.append(m.group(1))

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return sitemap_urls

    correct_urls = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True,
                                     args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        ctx = browser.new_context()
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")

        for isin in isins:
            try:
                test_url = f"{BASE_URL}/bond-details/{isin}/00000000-0000-0000-0000-000000000000/"
                page.goto(test_url, wait_until="domcontentloaded", timeout=15000)
                actual_url = page.url
                m = re.search(r'/bond-details/([A-Z0-9]+)/([a-f0-9\-]+)/', actual_url)
                if m and m.group(1) == isin:
                    correct_urls.append(f"{BASE_URL}/bond-details/{isin}/{m.group(2)}/")
                else:
                    matching = [u for u in sitemap_urls if isin in u]
                    if matching:
                        correct_urls.append(matching[0])
            except Exception:
                matching = [u for u in sitemap_urls if isin in u]
                if matching:
                    correct_urls.append(matching[0])

        browser.close()

    return correct_urls


# ─── Helpers ─────────────────────────────────────────────────────────────────

def parse_num(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[₹%,\s]", "", str(text))
    try:
        return float(cleaned)
    except ValueError:
        return None


def normalize_rating(text: str | None) -> str:
    if not text:
        return ""
    # Use (?!\w) not \b at the end: \b fails after non-word chars like - and +,
    # causing the regex to backtrack and return "AA" instead of "AA-".
    m = re.search(r"\b(AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|D)(?!\w)", str(text))
    return m.group(1) if m else str(text).strip()


def _backoff_wait(attempt: int) -> float:
    base = BACKOFF_BASE * (2 ** attempt)
    jitter = random.uniform(0, BACKOFF_JITTER)
    return min(base + jitter, BACKOFF_MAX)


_FINANCIAL_FIELDS = ("coupon_rate", "ytm", "face_value", "market_value")


def is_valid_bond(bond: dict) -> bool:
    name = str(bond.get("issuer_name") or "").strip()
    if not name or name == bond.get("isin", ""):
        return False
    populated = sum(1 for f in _FINANCIAL_FIELDS if bond.get(f))
    return populated >= 2




# ─── API JSON parsing ─────────────────────────────────────────────────────────

def extract_uuid_from_url(url: str) -> str:
    parts = url.rstrip("/").split("/")
    return parts[-1] if parts else ""


def _parse_api_json(api_response: dict, isin: str, url: str) -> dict:
    bond: dict = {
        "isin": isin,
        "url": url,
        "bond_platform": BOND_PLATFORM,
        "scraped_at": datetime.now().isoformat(),
    }
    security = api_response.get("security") or {}

    mappings = {
        "issuer_name":      [["issuer_object", "name"], ["issuer"], ["issuer_name"]],
        "coupon_rate":      [["coupon_rate"]],
        "ytm":              [["yield_value"], ["vendor_security_offer_price", 0, "yield_value"], ["nse_last_traded_yield"], ["ytm"]],
        "face_value":       [["face_value"]],
        "market_value":     [["market_value"]],
        "maturity_date":    [["maturity_date"]],
        "allotment_date":   [["allotment_date"]],
        "min_investment":   [["minimum_quantity"], ["minimum_qty"], ["minimum_investment"]],
        "payment_frequency":[["frequency_value"], ["frequency"], ["payment_frequency"]],
        "issue_size":       [["issue_size_in_crores"], ["issue_size"]],
        "mode":             [["mode_of_issue_value"], ["mode_of_issue"], ["mode"]],
        "security_status":  [["security_status_value"], ["security_status"], ["status"]],
        "issuer_type":      [["issuer_ownership_type_value"], ["issuer_ownership_type"], ["issuer_type"]],
        "security_class":   [["security_class_value"], ["security_class"]],
        "debenture_trustee":[["debenture_trustee_name"]],
    }

    def get_path(obj, path):
        for key in path:
            if isinstance(obj, dict):
                obj = obj.get(key)
            elif isinstance(obj, list) and isinstance(key, int) and 0 <= key < len(obj):
                obj = obj[key]
            else:
                return None
            if obj is None:
                return None
        return obj

    for our_key, key_alternatives in mappings.items():
        for key_path in key_alternatives:
            value = get_path(api_response, key_path)
            if value is None:
                value = get_path(security, key_path)
            if value is not None:
                bond[our_key] = value
                break

    security_ratings = (api_response.get("security_ratings") or security.get("security_ratings") or [])
    if security_ratings and isinstance(security_ratings, list):
        rating_obj = security_ratings[0]
        if isinstance(rating_obj, dict):
            bond["rating_raw"] = rating_obj.get("cra_rating", "")
            bond["rating_agency"] = rating_obj.get("credit_rating_agency_name", "")

    if not bond.get("rating_raw"):
        cra = (api_response.get("vendor_security_cra_rating") or security.get("vendor_security_cra_rating") or {})
        if isinstance(cra, dict) and cra.get("cra_rating"):
            bond["rating_raw"] = cra.get("cra_rating", "")
            bond["rating_agency"] = cra.get("credit_rating_agency_name", "") or bond.get("rating_agency", "")

    if not bond.get("rating_raw") and security.get("rating_from_json"):
        bond["rating_raw"] = security.get("rating_from_json", "")
        bond["rating_agency"] = security.get("rating_agency_from_json", "") or bond.get("rating_agency", "")

    if "rating_raw" in bond:
        bond["rating"] = normalize_rating(bond.pop("rating_raw"))
    elif "rating" not in bond:
        bond["rating"] = ""

    if not bond.get("rating"):
        sec_class = (bond.get("security_class") or "").lower()
        if any(x in sec_class for x in ["government", "state development", "sovereign"]):
            bond["rating"] = "Sovereign"
            bond["rating_agency"] = "Sovereign"

    for field in ("coupon_rate", "ytm", "face_value", "market_value", "min_investment"):
        if field in bond and isinstance(bond[field], str):
            bond[field] = parse_num(bond[field])

    return bond


# ─── Bond scraping ────────────────────────────────────────────────────────────

def scrape_bond(url: str, browser, logger: MongoScrapeLogger) -> dict:
    isin = url.split("/bond-details/")[1].split("/")[0]
    uuid = extract_uuid_from_url(url)
    api_endpoint = (f"{BASE_URL}/place-order/{isin}/{uuid}/calculate/"
                    "?include=custCashFlow&is_notify_me_inventory=false&ib_listing_status=false")

    started_mono = time.monotonic()

    for attempt in range(1, MAX_RETRIES + 1):
        wait = _backoff_wait(attempt - 1)
        logger.log_attempt(isin, attempt)

        ctx = None
        try:
            ctx = browser.new_context(**_PW_CONTEXT_OPTS)
            ctx.add_init_script(_STEALTH_JS)
            page = ctx.new_page()

            api_response_captured = []

            def on_response(resp):
                if "/place-order/" in resp.url and "calculate" in resp.url:
                    try:
                        api_response_captured.append(resp.body())
                    except Exception:
                        pass

            page.on("response", on_response)

            t0 = time.monotonic()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                page.wait_for_timeout(3000)
            elapsed_ms = (time.monotonic() - t0) * 1000

            if api_response_captured:
                status = 200
                body = api_response_captured[0]
            else:
                extracted = page.evaluate(r"""
                    () => {
                        const clean = (s) => (s || '').replace(/\u00a0/g, ' ')
                                                      .replace(/\s+/g, ' ').trim();
                        const result = {};

                        const h1 = document.querySelector('h1');
                        if (h1) result.issuer_name = clean(h1.textContent);

                        // Ratings blob rendered server-side into a JSON script tag.
                        const script = document.querySelector('script#mydata[type="application/json"]');
                        if (script && script.textContent) {
                            try {
                                const ratings = JSON.parse(script.textContent);
                                if (Array.isArray(ratings) && ratings.length > 0) {
                                    const r = ratings[0];
                                    // Prefer the most specific field — cra_rating may be just
                                    // the base grade ("AA") while cra_rating_value has the modifier ("AA-").
                                    const rv = r.cra_rating_value || r.cra_rating_symbol_value ||
                                               r.cra_rating_description || r.cra_rating;
                                    if (rv) {
                                        result.rating_from_json = clean(rv);
                                        result.rating_agency_from_json = r.credit_rating_agency_code_value;
                                    }
                                }
                            } catch(e) {}
                        }

                        // Label/value pairs: each field sits in an .info-value-div holding a
                        // .info-title-bd label and an .info-value-bd value. The label is not a
                        // leaf node (it wraps spans), so match on the container, not on leaves.
                        const labelMap = {};
                        for (const div of document.querySelectorAll('.info-value-div')) {
                            const label = div.querySelector('.info-title-bd');
                            const value = div.querySelector('.info-value-bd');
                            if (label && value) {
                                const k = clean(label.textContent).toLowerCase();
                                const v = clean(value.textContent);
                                if (k && v && v !== '-') labelMap[k] = v;
                            }
                        }

                        const pick = (...tests) => {
                            for (const t of tests) {
                                for (const [k, v] of Object.entries(labelMap)) {
                                    if (t(k)) return v;
                                }
                            }
                            return undefined;
                        };

                        result.coupon_rate       = pick(k => k.includes('coupon') && k.includes('rate'));
                        result.ytm               = pick(k => k.includes('ytm') || (k.includes('yield') && k.includes('maturity')));
                        result.maturity_date     = pick(k => k.includes('maturity') && k.includes('date'));
                        result.allotment_date    = pick(k => k.includes('allotment'));
                        result.payment_frequency = pick(k => k.includes('frequency'));
                        result.issue_size        = pick(k => k.includes('issue size'));
                        result.security_class    = pick(k => k.includes('security class'));
                        result.debenture_trustee = pick(k => k.includes('debenture trustee'));
                        result.issuer_type       = pick(k => k.includes('ownership'));
                        if (!result.rating_from_json) {
                            const r = pick(k => k === 'rating' || k.includes('rating category'));
                            if (r) result.rating_from_json = r;
                        }

                        // Face/market value live in the order calculator, keyed by id.
                        const byId = (id) => {
                            const e = document.getElementById(id);
                            return e ? clean(e.textContent) : undefined;
                        };
                        result.face_value   = byId('calculated_face_value');
                        result.market_value = byId('calculated_market_price_value');

                        for (const k of Object.keys(result)) {
                            if (result[k] === undefined || result[k] === '') delete result[k];
                        }
                        return result;
                    }
                """)
                page.close()
                if extracted.get("issuer_name") or extracted.get("coupon_rate"):
                    status = 200
                    body = json.dumps({"security": extracted}).encode("utf-8")
                else:
                    status = 500
                    body = b"{}"

            logger.log_http(isin, attempt, status, len(body), elapsed_ms)
            page.close()

            if status in (429, 503):
                logger.log_rate_limit(isin, attempt, status, wait)
                time.sleep(wait)
                continue

            if status == 200:
                try:
                    api_data = json.loads(body.decode("utf-8"))
                    bond = _parse_api_json(api_data, isin, url)
                    if is_valid_bond(bond):
                        logger.log_success(isin, bond, started_mono)
                        return bond
                    if attempt < MAX_RETRIES:
                        logger.log_empty(isin, attempt, wait)
                        time.sleep(wait)
                    else:
                        bond["_incomplete"] = True
                        logger.log_incomplete(isin, started_mono)
                        return bond
                except (json.JSONDecodeError, ValueError) as exc:
                    if attempt < MAX_RETRIES:
                        logger.log_network_error(isin, attempt, str(exc), wait)
                        time.sleep(wait)
                    else:
                        logger.log_failure(isin, f"JSON parse error: {exc}", started_mono)
                        raise
            else:
                err = f"HTTP {status}"
                if attempt < MAX_RETRIES:
                    logger.log_network_error(isin, attempt, err, wait)
                    time.sleep(wait)
                else:
                    logger.log_failure(isin, err, started_mono)
                    raise Exception(err)

        except Exception as exc:
            err = str(exc)
            if attempt < MAX_RETRIES:
                logger.log_network_error(isin, attempt, err, wait)
                time.sleep(wait)
            else:
                logger.log_failure(isin, err, started_mono)
                raise
        finally:
            if ctx:
                try:
                    ctx.close()
                except Exception:
                    pass

    return {"isin": isin, "url": url, "bond_platform": BOND_PLATFORM,
            "_incomplete": True, "scraped_at": datetime.now().isoformat()}


# ─── MongoDB helpers ──────────────────────────────────────────────────────────

def _load_checkpoint(bonds_col) -> dict[str, dict]:
    """Load previously scraped bonds from MongoDB bonds collection."""
    done = {}
    try:
        for doc in bonds_col.find({"bond_platform": BOND_PLATFORM}, {"_id": 0}):
            if "isin" in doc:
                done[doc["isin"]] = doc
    except Exception:
        pass
    return done


def _upsert_bond(bonds_col, bond: dict) -> None:
    """Upsert a single bond into the bonds collection keyed by ISIN.
    Uses $set so valuation fields (verdict_rating, report_pdf_b64, etc.) are preserved."""
    bonds_col.update_one({"isin": bond["isin"]}, {"$set": bond}, upsert=True)


# ─── Main entry point ─────────────────────────────────────────────────────────

def scrape_all_to_mongo(task_id: str = "", resume: bool = True,
                        limit: int | None = None) -> dict:
    """
    Entry point called by the Celery task.
    Uses pymongo (sync) for all DB writes so it runs fine in a Celery worker.
    """
    client = pymongo.MongoClient(MONGO_URL)
    db = client[DB_NAME]
    bonds_col = db["bonds"]
    log = MongoScrapeLogger(db, task_id=task_id)

    done: dict[str, dict] = _load_checkpoint(bonds_col) if resume else {}
    if resume:
        log.load_from_db()

    log.run_start(0)

    urls = get_bond_urls()
    if limit:
        urls = urls[:limit]
    total = len(urls)

    log._run["total_urls"] = total
    log._flush()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("playwright not installed — run: pip3 install playwright && playwright install chromium")

    skipped = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=_PW_LAUNCH_ARGS)
        try:
            for i, url in enumerate(urls, 1):
                isin = url.split("/bond-details/")[1].split("/")[0]

                if isin in done and not done[isin].get("_incomplete"):
                    log.log_skip(isin, url)
                    skipped += 1
                    continue

                log.bond_start(isin, url)

                try:
                    bond = scrape_bond(url, browser, log)
                    done[isin] = bond
                    _upsert_bond(bonds_col, bond)
                except Exception as e:
                    print(f"FAILED {isin}: {e}")

                if i < total:
                    time.sleep(REQUEST_DELAY)
        finally:
            browser.close()

    log.run_end()
    client.close()

    success  = sum(1 for b in done.values() if not b.get("_incomplete"))
    failed   = sum(1 for b in done.values() if b.get("_incomplete"))
    return {
        "bonds_scraped": len(done),
        "success": success,
        "failed": failed,
        "skipped": skipped,
    }
