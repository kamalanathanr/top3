"""MakeMyTrip flight scraper for Top3.

Why this attaches to a real Chrome instead of launching its own
--------------------------------------------------------------
MMT sits behind Akamai bot protection that rejects Playwright's *own* browser
at the network layer - the request is reset with ERR_HTTP2_PROTOCOL_ERROR
before a page ever loads. That is not an IP block (a normal browser on the same
machine works fine); it is the fingerprint of a freshly-launched, cookieless,
automation-flagged browser.

So this module does not launch a browser. It starts (or reuses) an ordinary
Chrome with a remote-debugging port and a persistent profile, then attaches to
it over CDP. That Chrome is indistinguishable from the one you browse with, and
its cookies accumulate across runs, which keeps it trusted.

Two things that must NOT be "fixed":
  - Do not override the user-agent. A spoofed UA that disagrees with the real
    Chrome build is itself a bot signal.
  - Do not run headless. Headless is rejected outright.

    pip install playwright     # the Chrome you already have is used as-is

Usage:
    from src.flights import search_flights
    flights = search_flights("BLR", "DEL", "2026-10-15", travellers=2, limit=3)

    python -m src.flights BLR DEL 2026-10-15 --travellers 2 --limit 3
    python -m src.flights BLR DEL 2026-10-15 --sort fastest
    python -m src.flights BLR DEL 2026-10-15 --sort non_stop
"""

import argparse
import json
import os
import re
import socket
import sys
import subprocess
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta

HOME_URL = "https://www.makemytrip.com/"

SEARCH_URL = (
    "https://www.makemytrip.com/flight/search"
    "?itinerary={src}-{dst}-{date}&tripType=O&paxType=A-{adults}_C-0_I-0"
    "&cabinClass=E&lang=eng"
)

# Return fares are usually cheaper than two one-ways, so a round trip is
# searched as one tripType=R itinerary rather than two separate legs.
RETURN_URL = (
    "https://www.makemytrip.com/flight/search"
    "?itinerary={src}-{dst}-{date}_{dst}-{src}-{ret}&tripType=R"
    "&paxType=A-{adults}_C-0_I-0&cabinClass=E&lang=eng"
)

CHROME_BIN = os.environ.get(
    "CHROME_BIN", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)
PROFILE_DIR = os.environ.get(
    "TOP3_CHROME_PROFILE", os.path.expanduser("~/.top3-chrome-profile")
)
CDP_PORT = int(os.environ.get("TOP3_CDP_PORT", "9222"))

# Set once the homepage has been loaded in this process.
_WARMED = False

# Only one scrape may drive the shared browser at a time; concurrent runs
# closed each other's tabs and produced TargetClosedError.
LOCK_PATH = os.path.join(PROFILE_DIR, ".scrape.lock")


class RateLimited(RuntimeError):
    """MMT refused the connection outright - almost always rate limiting.

    Shows up as ERR_HTTP2_PROTOCOL_ERROR: the request is reset before any page
    loads. It clears on its own after a cool-off, so callers should back off
    rather than retry immediately or treat it as "no flights".
    """

# Each result row. The outer ".listingCardWrap" is the sort-summary banner and
# is deliberately excluded.
CARD_SELECTOR = "div.listingCardItem"

RE_PRICE = re.compile(r"₹\s?([\d,]{3,})")
RE_TIME = re.compile(r"\b([0-2]?\d:[0-5]\d)\b")
RE_DURATION = re.compile(r"\b(\d{1,2})\s?h\s*(?:(\d{1,2})\s?m)?\b")
RE_STOPS = re.compile(r"(non[\s-]?stop|(\d)\s?stops?)", re.I)
RE_AIRPORT = re.compile(r"^[A-Z]{3}$")
# MMT sometimes renders a transient "network problem" panel with a REFRESH
# button instead of results. Waiting and refreshing in-page recovers it; a
# fresh navigation often does not.
RE_NETERROR = re.compile(
    r"network\s+problem|something\s+went\s+wrong|try\s+again|"
    r"unable\s+to\s+(?:fetch|load)|slow\s+internet",
    re.I)
RE_NOFLIGHTS = re.compile(
    r"Flights?\s+Not\s+Found|could not find flights|no flights found",
    re.I)
# "Cabin \n 7 KGS/Adult \n Check-in \n 20 KGS/Adult", per segment.
RE_BAGGAGE = re.compile(
    r"Cabin\s*\n+\s*([^\n]+?)\s*\n+\s*Check-?in\s*\n+\s*([^\n]+)", re.I)
RE_REFUND = re.compile(r"\b((?:non[\s-]?|partially\s+)?refundable)\b", re.I)
RE_LAYOVER = re.compile(
    r"(?:(\d+)\s*hrs?)?\s*(?:(\d+)\s*mins?)?\s*Layover(?:\s*•\s*"
    r"(?:Change of Plane at|Same Plane at)?\s*([^\n]+))?", re.I)
# depart time / date / duration / arrive time / date, as printed per segment
RE_SEGMENT = re.compile(
    r"(\d{1,2}:\d{2})\s*\n+\s*(\w{3},\s*\d{1,2}\s*\w{3})\s*\n+\s*"
    r"(\d{1,2})h\s*(\d{1,2})m\s*\n+\s*(\d{1,2}:\d{2})\s*\n+\s*"
    r"(\w{3},\s*\d{1,2}\s*\w{3})")
RE_SEGFLIGHT = re.compile(r"\b([A-Z0-9]{2}[\s-]?\d{2,5})\s*\(([^)]+)\)")
RE_PLUSDAY = re.compile(r"\+\s?(\d)\s?DAY", re.I)
RE_BADGE = re.compile(r"%|on time|cheapest|non stop first|you may prefer", re.I)
RE_FLIGHTNO = re.compile(
    r"^[A-Z0-9]{2}[\s-]?\d{2,5}([\s,|]+[A-Z0-9]{2}[\s-]?\d{2,5})*$"
)


@dataclass
class Flight:
    airline: str
    flight_number: str
    price: int
    price_basis: str
    currency: str
    depart_time: str
    arrive_time: str
    duration: str
    duration_minutes: int
    stops: int
    stop_via: str
    arrives_next_day: int
    source: str
    destination: str
    date: str
    search_url: str = ""
    cabin_baggage: str = ""
    checkin_baggage: str = ""
    refundable: str = ""
    layover_minutes: int = 0
    layovers: list = field(default_factory=list)
    segments: list = field(default_factory=list)
    is_alternative: bool = False
    route: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def _fmt_date(date: str) -> str:
    """Accept YYYY-MM-DD, DD/MM/YYYY or DD-MM-YYYY -> DD/MM/YYYY."""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(date, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date: {date!r} (use YYYY-MM-DD)")


def _hm(minutes: int) -> str:
    return f"{minutes // 60}h {minutes % 60:02d}m"


# --------------------------------------------------------------------------
# Chrome / CDP plumbing
# --------------------------------------------------------------------------

def _port_open(port, host="127.0.0.1"):
    with socket.socket() as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) == 0


class _BrowserLock:
    """Serialises access to the shared Chrome across processes.

    Two runs driving the same browser closed each other's tabs, surfacing as
    TargetClosedError. This makes a second run wait rather than interfere.
    """

    def __init__(self, path=None, timeout=900):
        self.path = path or LOCK_PATH
        self.timeout = timeout
        self.fh = None

    def _holder_alive(self):
        try:
            with open(self.path) as fh:
                pid = int((fh.read() or "0").strip() or 0)
        except (OSError, ValueError):
            return False
        if not pid:
            return False
        if pid == os.getpid():
            return True          # our own lock: held, not stale
        try:
            os.kill(pid, 0)          # signal 0 just tests existence
            return True
        except OSError:
            return False

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        waited = 0
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                if not self._holder_alive():
                    # Stale lock from a killed run.
                    try:
                        os.remove(self.path)
                    except OSError:
                        pass
                    continue
                if waited >= self.timeout:
                    raise RuntimeError(
                        f"Another flight search has held the browser for "
                        f"{waited}s ({self.path}). Wait, or delete the lock "
                        f"if nothing is running."
                    )
                time.sleep(2)
                waited += 2

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except OSError:
            pass
        return False


def _close_stray_pages(ctx, keep=None):
    """One tab at a time: close anything left over from an earlier search."""
    for pg in list(ctx.pages):
        if pg is keep:
            continue
        try:
            pg.close()
        except Exception:
            pass


def _owns_port(port, profile_dir):
    """True if the Chrome on `port` is one we started with `profile_dir`.

    Attaching to some other Chrome - the user's everyday browser, say - fails
    later with "Browser context management is not supported", so check first.
    """
    try:
        out = subprocess.run(
            ["pgrep", "-fl", f"remote-debugging-port={port}"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return False
    return any(profile_dir in line for line in out.splitlines())


def _free_port(start, profile_dir, tries=10):
    """First port that is either free or already ours."""
    for port in range(start, start + tries):
        if not _port_open(port) or _owns_port(port, profile_dir):
            return port
    raise RuntimeError(
        f"No usable debugging port in {start}-{start + tries - 1}."
    )


def _ensure_chrome(port, profile_dir, wait=25):
    """Start Chrome with a debugging port if one of ours isn't already up."""
    if _port_open(port) and _owns_port(port, profile_dir):
        return False
    if _port_open(port):
        raise RuntimeError(
            f"Port {port} is held by a Chrome that is not ours (probably your "
            f"everyday browser). Use a different port: "
            f"TOP3_CDP_PORT={port + 1} or --port {port + 1}."
        )

    if not os.path.exists(CHROME_BIN):
        raise RuntimeError(
            f"Chrome not found at {CHROME_BIN}. Set CHROME_BIN to its path."
        )
    os.makedirs(profile_dir, exist_ok=True)
    subprocess.Popen(
        [
            CHROME_BIN,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(wait * 2):
        if _port_open(port):
            return True
        time.sleep(0.5)
    raise RuntimeError(f"Chrome did not open a debugging port on {port}")


def _kill_ours(port, profile_dir):
    """Terminate only the Chrome we launched for this profile."""
    try:
        subprocess.run(
            ["pkill", "-f", f"user-data-dir={profile_dir}"],
            capture_output=True, timeout=5,
        )
        time.sleep(2)
    except Exception:
        pass


def _refresh(page, wait_before=6000, settle=4000):
    """Recover MMT's transient "network problem" panel.

    It ships a REFRESH button; waiting a moment and clicking it brings the
    results back, where re-navigating often just reproduces the error. Falls
    back to a page reload when the button cannot be found.
    """
    page.wait_for_timeout(wait_before)
    for sel in ("text=REFRESH", "button:has-text('REFRESH')",
                "[class*='refresh']", "text=Try Again",
                "button:has-text('Try Again')"):
        try:
            page.click(sel, timeout=2500)
            page.wait_for_timeout(settle)
            return True
        except Exception:
            continue
    try:
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(settle)
        return True
    except Exception:
        return False


def _goto(page, url, timeout):
    """Navigate, translating MMT's rate-limit reset into RateLimited."""
    try:
        page.goto(url, timeout=timeout, wait_until="domcontentloaded")
    except Exception as e:
        if "ERR_HTTP2_PROTOCOL_ERROR" in str(e) or "ERR_CONNECTION" in str(e):
            raise RateLimited(
                "MakeMyTrip refused the connection (rate limited). Wait a few "
                "minutes before retrying; the block clears on its own."
            ) from e
        raise


def _dismiss_modal(page):
    """MMT drops a login/offer overlay over the results."""
    for sel in ("span.commonModal__close", ".commonModal__close",
                "[data-cy='closeModal']"):
        try:
            page.click(sel, timeout=2000)
            return
        except Exception:
            continue
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _parse_card(text, src, dst, date, currency="INR"):
    """Turn one listing card's rendered text into a Flight.

    A card looks like:

        IndiGo
        6E-6003 | 6E-6434
        15:35
        BLR
        04h 55m
        1 stop • NAG
        20:30
        DEL
        ₹ 8,170
        VIEW PRICES ...

    Text is parsed rather than per-field CSS because MMT's class names are
    hashed and rotate; the visible text does not.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None

    # Fare comes before the promo blurbs ("Get Flat Rs 180 OFF", "Lock @ 179"),
    # so cut the tail before matching price.
    head = text.split("VIEW PRICES")[0] or text
    prices = RE_PRICE.findall(head)
    times = RE_TIME.findall(text)
    if not prices or len(times) < 2:
        return None
    # MMT prints the struck-through fare first and the payable one second.
    price = int(prices[-1].replace(",", ""))
    per_adult = "/adult" in head

    stops_m = RE_STOPS.search(text)
    stops = int(stops_m.group(2)) if stops_m and stops_m.group(2) else 0

    stop_via = ""
    for ln in lines:
        if "•" in ln and "stop" in ln.lower():
            stop_via = ln.split("•", 1)[1].strip()
            break

    day_m = RE_PLUSDAY.search(text)
    arrives_next_day = int(day_m.group(1)) if day_m else 0

    dur_m = RE_DURATION.search(text)
    minutes = (int(dur_m.group(1)) * 60 + int(dur_m.group(2) or 0)) if dur_m else 10 ** 6

    codes = [ln for ln in lines if RE_AIRPORT.match(ln)]

    # The carrier name sits directly above the flight-number line. Taking
    # lines[0] instead picks up badges like "100% on time" or "Cheapest".
    flight_no, airline = "", ""
    for i, ln in enumerate(lines):
        if RE_FLIGHTNO.match(ln):
            flight_no = ln
            if i:
                airline = lines[i - 1]
            break
    if not airline:
        airline = next(
            (ln for ln in lines if not RE_BADGE.search(ln)), lines[0]
        )

    refund_m = RE_REFUND.search(text)

    return Flight(
        refundable=refund_m.group(1).title() if refund_m else "",
        airline=airline[:60],
        flight_number=flight_no,
        price=price,
        price_basis="per adult" if per_adult else "total",
        currency=currency,
        depart_time=times[0],
        arrive_time=times[1],
        duration=_hm(minutes) if dur_m else "",
        duration_minutes=minutes,
        stops=stops,
        stop_via=stop_via,
        arrives_next_day=arrives_next_day,
        source=codes[0] if codes else src.upper(),
        destination=codes[-1] if len(codes) > 1 else dst.upper(),
        date=date,
        route=codes,
    )


def _parse_details(text, codes):
    """Parse the expanded 'Flight Details' panel into segments and layovers.

    MMT prints, per leg: departure time, departure date, leg duration, arrival
    time, arrival date - then the carrier and aircraft. Between legs it prints
    e.g. "2 hrs 30 mins Layover - Change of Plane at Kuala Lumpur".

    `codes` is the airport sequence (origin, each stop, destination) so legs can
    be labelled without re-deriving them from the city names.
    """
    body = text.split("Flight Details", 1)[-1]

    segments = []
    for i, m in enumerate(RE_SEGMENT.finditer(body)):
        dep_t, dep_d, hh, mm, arr_t, arr_d = m.groups()
        segments.append({
            "from": codes[i] if i < len(codes) else "",
            "to": codes[i + 1] if i + 1 < len(codes) else "",
            "depart_time": dep_t,
            "depart_date": dep_d,
            "arrive_time": arr_t,
            "arrive_date": arr_d,
            "duration_minutes": int(hh) * 60 + int(mm),
            "duration": _hm(int(hh) * 60 + int(mm)),
        })

    flights_seen = RE_SEGFLIGHT.findall(body)
    for seg, (fno, aircraft) in zip(segments, flights_seen):
        seg["flight_number"] = fno.strip()
        seg["aircraft"] = aircraft.strip()

    layovers = []
    for m in RE_LAYOVER.finditer(body):
        hrs, mins, where = m.groups()
        if not hrs and not mins:
            continue
        minutes = int(hrs or 0) * 60 + int(mins or 0)
        layovers.append({
            "airport": (where or "").strip(),
            "code": codes[len(layovers) + 1] if len(layovers) + 1 < len(codes) else "",
            "duration_minutes": minutes,
            "duration": _hm(minutes),
        })

    # Prefer the layover MMT states; fall back to the gap between legs.
    if not layovers and len(segments) > 1:
        for a, b in zip(segments, segments[1:]):
            gap = _gap_minutes(a["arrive_time"], b["depart_time"])
            layovers.append({
                "airport": "", "code": a["to"],
                "duration_minutes": gap, "duration": _hm(gap),
            })

    for seg, m in zip(segments, RE_BAGGAGE.finditer(body)):
        seg["cabin_baggage"] = m.group(1).strip()
        seg["checkin_baggage"] = m.group(2).strip()

    return segments, layovers


def _gap_minutes(arrive, depart):
    """Minutes between an arrival and the next departure, wrapping past midnight."""
    a = datetime.strptime(arrive, "%H:%M")
    d = datetime.strptime(depart, "%H:%M")
    delta = int((d - a).total_seconds() // 60)
    return delta + 1440 if delta < 0 else delta


def _sort(flights, sort):
    """Order results. Returns (flights, note).

    "non_stop" on a route with no direct service falls back to the next best
    thing - fewest stops, then price - rather than returning nothing.
    """
    if sort == "fastest":
        return sorted(flights, key=lambda f: (f.duration_minutes, f.price)), ""

    if sort == "non_stop":
        direct = [f for f in flights if f.stops == 0]
        if direct:
            return sorted(direct, key=lambda f: f.price), ""
        if not flights:
            return [], ""
        fewest = min(f.stops for f in flights)
        alts = sorted(flights, key=lambda f: (f.stops, f.price))
        for f in alts:
            f.is_alternative = True
        return alts, (
            f"No non-stop flights on this route. "
            f"Showing best alternatives ({fewest} stop"
            f"{'s' if fewest != 1 else ''} minimum), fewest stops first."
        )

    return sorted(flights, key=lambda f: f.price), ""


# --------------------------------------------------------------------------

def _find_card(page, marker):
    """Locate a card by a text marker unique to it.

    The list is virtualised and expanding one card reflows the rest, so indices
    go stale between expansions; matching on content is stable.
    """
    for card in page.query_selector_all(CARD_SELECTOR):
        try:
            if marker in card.inner_text():
                return card
        except Exception:
            continue
    return None


def _expand_details(page, marker, timeout=8000):
    """Open one card's detail panel and return its text ("" if it won't open)."""
    try:
        page.evaluate(
            "document.querySelectorAll(\"[class*='coachmark']\")"
            ".forEach(e => e.remove())"
        )
        card = _find_card(page, marker)
        if not card:
            return ""
        card.scroll_into_view_if_needed(timeout=timeout)
        page.wait_for_timeout(400)
        link = card.query_selector("text=More flight details")
        if not link:
            return ""
        link.click(timeout=timeout)
        page.wait_for_timeout(2200)

        card = _find_card(page, marker)
        text = card.inner_text() if card else ""

        # Collapse again so the next card's position stays predictable.
        try:
            card.query_selector("text=Hide flight details").click(timeout=3000)
            page.wait_for_timeout(600)
        except Exception:
            pass
        return text
    except Exception:
        return ""


def search_flights(source, destination, date, travellers=1, limit=3,
                   sort="cheapest", currency="INR", timeout=90000,
                   port=CDP_PORT, profile_dir=PROFILE_DIR, keep_open=True,
                   details=True, retries=3):
    """Scrape MakeMyTrip one-way flights.

    sort: "cheapest" (price), "fastest" (duration), "non_stop" (direct only,
          then price). Applied locally over everything scraped.
    Returns a list of dicts, at most `limit` long.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError("Playwright is required: pip install playwright") from e

    day = _fmt_date(date)
    url = SEARCH_URL.format(
        src=source.upper(), dst=destination.upper(),
        date=day, adults=max(1, int(travellers)),
    )

    port = _free_port(port, profile_dir)
    _ensure_chrome(port, profile_dir)

    with _BrowserLock(), sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        except Exception:
            # Stale or unusable browser on the port - replace ours and retry.
            _kill_ours(port, profile_dir)
            _ensure_chrome(port, profile_dir)
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        # One tab at a time: clear anything an earlier search left behind.
        _close_stray_pages(ctx)
        cards, flights = [], []
        last_error = None
        # Under rapid sequential load MMT intermittently renders no cards on a
        # page that is otherwise fine; the same search succeeds on a retry. So
        # retry on a fresh page rather than reporting a failure.
        for attempt in range(retries):
            page = ctx.new_page()
            try:
                global _WARMED
                if not _WARMED:
                    _goto(page, HOME_URL, timeout)
                    page.wait_for_timeout(4000)
                    _dismiss_modal(page)
                    _WARMED = True

                _goto(page, url, timeout)

                cards = []
                refreshes = 0
                for _ in range(14):
                    page.wait_for_timeout(2500)
                    cards = page.eval_on_selector_all(
                        CARD_SELECTOR, "els => els.map(e => e.innerText)"
                    )
                    if len(cards) >= 3:
                        break
                    _dismiss_modal(page)
                    # A transient network panel needs REFRESH, not more waiting.
                    if not cards and refreshes < 3:
                        try:
                            body = page.inner_text("body")
                        except Exception:
                            body = ""
                        if RE_NETERROR.search(body):
                            refreshes += 1
                            _refresh(page)

                if not cards:
                    body = page.inner_text("body")
                    if RE_NOFLIGHTS.search(body):
                        # A real answer, not a failure: MMT operates nothing
                        # between these two airports. Flagged so callers can
                        # remember the dead route instead of re-searching it.
                        search_flights.last_status = "no_flights"
                        return []
                    if len(body) < 1000:
                        # A stub can also clear on an in-page refresh, so try
                        # that before calling it a block.
                        _refresh(page, wait_before=8000)
                        body = page.inner_text("body")
                        cards = page.eval_on_selector_all(
                            CARD_SELECTOR, "els => els.map(e => e.innerText)"
                        )
                        if not cards and len(body) < 1000:
                            raise RuntimeError(
                                "MakeMyTrip served a bot-wall stub. Run "
                                "'python -m src.flights --diagnose' to check."
                            )
                    # Last resort: refresh in place before discarding the page.
                    if _refresh(page):
                        for _ in range(8):
                            page.wait_for_timeout(2500)
                            cards = page.eval_on_selector_all(
                                CARD_SELECTOR,
                                "els => els.map(e => e.innerText)"
                            )
                            if cards:
                                break
                if not cards:
                    last_error = RuntimeError(
                        "No flight cards found - page loaded but rendered no "
                        f"results (selector {CARD_SELECTOR!r})."
                    )
                    page.close()
                    page = None
                    if attempt < retries - 1:
                        time.sleep(4 * (attempt + 1))
                        continue
                    raise last_error

                seen, flights = set(), []
                for i, text in enumerate(cards):
                    f = _parse_card(text or "", source, destination, day,
                                    currency)
                    if not f:
                        continue
                    key = (f.airline, f.flight_number, f.depart_time, f.price)
                    if key in seen:
                        continue
                    seen.add(key)
                    f.search_url = url
                    f._marker = (text or "").strip().split("\n")[1][:40]
                    flights.append(f)

                ranked, note = _sort(flights, sort)

                # Expand only what we return - each click costs a few seconds.
                if details:
                    for f in ranked[:limit]:
                        codes = [f.source] + [
                            c.strip() for c in f.stop_via.split(",")
                            if c.strip()
                        ] + [f.destination]
                        detail = _expand_details(page, f._marker)
                        if not detail:
                            continue
                        segs, lays = _parse_details(detail, codes)
                        f.segments = segs
                        f.layovers = lays
                        f.layover_minutes = sum(
                            l["duration_minutes"] for l in lays)
                        if segs:
                            f.cabin_baggage = segs[0].get("cabin_baggage", "")
                            f.checkin_baggage = segs[0].get(
                                "checkin_baggage", "")
                        if not f.refundable:
                            m = RE_REFUND.search(detail)
                            f.refundable = m.group(1).title() if m else ""
                break
            except RateLimited:
                raise
            except Exception as e:
                # A closed target means the browser went away - reconnect next
                # attempt rather than failing the whole search.
                last_error = e
                if attempt >= retries - 1:
                    raise
                time.sleep(4 * (attempt + 1))
            finally:
                if page is not None:
                    try:
                        page.close()
                    except Exception:
                        pass
        if not keep_open:
            browser.close()

    return [f.to_dict() for f in ranked[:limit]]


def diagnose(port=CDP_PORT, profile_dir=PROFILE_DIR):
    """Report what MakeMyTrip is actually doing right now.

    Answers "is it really blocked, or is my code broken?" - the two look
    identical from a failed run. Checks each layer separately and prints what
    each one saw, so the answer is evidence rather than inference.
    """
    from playwright.sync_api import sync_playwright

    print("MakeMyTrip reachability check")
    print("-" * 60)

    # Layer 1: plain HTTP. A 403 here is normal and expected - curl has a
    # non-browser TLS fingerprint - so it proves nothing on its own.
    try:
        import subprocess as sp
        out = sp.run(["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
                      "--max-time", "20", "https://www.makemytrip.com/"],
                     capture_output=True, text=True, timeout=30).stdout.strip()
        print(f"curl (non-browser)   : HTTP {out or 'no response'}"
              f"   <- 403/000 here is normal, not a verdict")
    except Exception as e:
        print(f"curl (non-browser)   : failed ({type(e).__name__})")

    # Layer 2: the real browser this scraper drives. This is the one that
    # matters, and it is a different profile from your everyday Chrome.
    try:
        used = _free_port(port, profile_dir)
        _ensure_chrome(used, profile_dir)
    except Exception as e:
        print(f"chrome               : cannot start ({e})")
        return 1

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{used}")
        except Exception as e:
            print(f"chrome (cdp {used})   : cannot attach ({str(e)[:60]})")
            return 1
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        _close_stray_pages(ctx)
        page = ctx.new_page()
        verdict = 0
        try:
            for label, url in (
                ("homepage", HOME_URL),
                ("search page", SEARCH_URL.format(
                    src="BLR", dst="DEL",
                    date=(datetime.now() + timedelta(days=30)).strftime(
                        "%d/%m/%Y"),
                    adults=1)),
            ):
                try:
                    r = page.goto(url, timeout=45000,
                                  wait_until="domcontentloaded")
                    page.wait_for_timeout(4000)
                    body = page.inner_text("body")
                    status = r.status if r else "?"
                    note = ""
                    if label == "search page":
                        cards = len(page.query_selector_all(CARD_SELECTOR))
                        if not cards:
                            # Match the scraper: a transient panel or stub
                            # often clears on an in-page refresh.
                            _refresh(page, wait_before=8000)
                            page.wait_for_timeout(4000)
                            body = page.inner_text("body")
                            cards = len(page.query_selector_all(CARD_SELECTOR))
                            if cards:
                                print("                       (recovered after "
                                      "REFRESH)")
                        if cards:
                            note = f"{cards} flight cards -> WORKING"
                        elif RE_NOFLIGHTS.search(body):
                            note = "MMT says no flights on this route/date"
                        elif len(body) < 1000:
                            note = "stub page -> BLOCKED"
                            verdict = 1
                        else:
                            note = "loaded but no cards -> markup may have changed"
                            verdict = 1
                    print(f"chrome {label:<14}: HTTP {status} "
                          f"{len(body):>6} bytes  {note}")
                except Exception as e:
                    msg = str(e)
                    if "ERR_HTTP2_PROTOCOL_ERROR" in msg:
                        print(f"chrome {label:<14}: CONNECTION RESET before "
                              f"any page loaded -> BLOCKED")
                        print("                       (a normal browser does "
                              "not get this; wait and retry)")
                    else:
                        print(f"chrome {label:<14}: {type(e).__name__} "
                              f"{msg[:60]}")
                    verdict = 1
        finally:
            page.close()

    print("-" * 60)
    print("Note: this uses profile", profile_dir)
    print("Your everyday Chrome has different cookies, so it can succeed")
    print("while this one is throttled. They are not the same session.")
    return verdict


def main():
    ap = argparse.ArgumentParser(description="Scrape MakeMyTrip flights")
    ap.add_argument("--diagnose", action="store_true",
                    help="check whether MMT is reachable right now, and exit")
    ap.add_argument("source", nargs="?", help="origin airport code, e.g. BLR")
    ap.add_argument("destination", nargs="?",
                    help="destination airport code, e.g. DEL")
    ap.add_argument("date", nargs="?", help="travel date, YYYY-MM-DD")
    ap.add_argument("--travellers", type=int, default=1)
    ap.add_argument("--limit", type=int, default=3, help="best N options")
    ap.add_argument("--sort", default="cheapest",
                    choices=["cheapest", "fastest", "non_stop"])
    ap.add_argument("--no-details", action="store_true",
                    help="skip per-leg layover/segment expansion (faster)")
    ap.add_argument("--port", type=int, default=CDP_PORT)
    args = ap.parse_args()

    if args.diagnose:
        raise SystemExit(diagnose(port=args.port))
    if not (args.source and args.destination and args.date):
        ap.error("source, destination and date are required "
                 "(or use --diagnose)")

    results = search_flights(
        args.source, args.destination, args.date,
        travellers=args.travellers, limit=args.limit,
        sort=args.sort, port=args.port, details=not args.no_details,
    )
    note = getattr(search_flights, "last_note", "")
    if note:
        print(f"\n  {note}\n", file=sys.stderr)
    elif not results:
        print(f"\n  No flights found for {args.source.upper()}-"
              f"{args.destination.upper()} on {args.date}.\n", file=sys.stderr)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
