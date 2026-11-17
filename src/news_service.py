"""
News aggregator — pulls the latest headlines from public RSS feeds.

Feeds are fetched in parallel and normalised into a common item shape.
Each item gets a ranking score (recency + importance) and is upserted into
the `news_items` collection; the page reads back sorted by score.

Liked stories are copied into `news_saved`, which retention cleanup and the
news_items TTL never touch, so they stay for future reference.

User settings (user_profiles.news_preferences):
  retention_days   news older than this is shown nowhere and deleted
  refresh_minutes  how often news is refetched — see src/news_refresh.py for
                   who triggers a refresh (all fetches run in Celery)

Scoring (0-100):
  base_score — general importance, see _score_items():
    recency   45%  exponential decay, half-life RECENCY_HALF_LIFE_HOURS
    coverage  30%  how many *other* publishers carry the same story
    keywords  15%  market / macro / major-event terms in the headline
    source    10%  tier-1 publishers rank slightly higher
  score — personalised, see _apply_preferences(): once the user has set
    news preferences (user_profiles.news_preferences), score blends
    base_score with an interest match (INTEREST_WEIGHT). Muted sources /
    keywords are flagged `muted` and left out of the feed.

Sources without a working public RSS feed are read via Google News filtered
to that site: Moneycontrol, WSJ and CNN's own feeds stopped updating
(2023-2025), Dinamalar / Goodreturns / Forbes India / Investopedia have none,
and TradingView's feed is trader chart ideas rather than news.
"""

import asyncio
import html
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import requests
from pymongo import ReplaceOne

RETENTION_OPTIONS = [1, 2, 3, 5, 7]          # days — user-selectable
REFRESH_OPTIONS = [5, 10, 15, 30, 60]         # minutes — user-selectable
MAX_RETENTION_DAYS = max(RETENTION_OPTIONS)   # TTL on news_items.published_at (safety net)
RECENCY_HALF_LIFE_HOURS = 6
MAX_ITEMS_PER_FEED = 30
REQUEST_TIMEOUT = 12

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def _google_news_search(query: str, lang: str = "en-IN", country: str = "IN", window: str = "3d") -> str:
    """Google News for a topic (not one site) — used where no single feed covers it."""
    ceid_lang = lang.split("-")[0]
    return (
        f"https://news.google.com/rss/search?q={quote_plus(query)}+when:{window}"
        f"&hl={lang}&gl={country}&ceid={country}:{ceid_lang}"
    )


def _google_news(site: str, lang: str = "en-IN", country: str = "IN") -> str:
    ceid_lang = lang.split("-")[0]
    return (
        f"https://news.google.com/rss/search?q=site:{site}+when:1d"
        f"&hl={lang}&gl={country}&ceid={country}:{ceid_lang}"
    )


# category is used for the filter chips on the News page
NEWS_SOURCES = [
    # India
    {"name": "Times of India",    "category": "India",    "url": "https://timesofindia.indiatimes.com/rssfeedstopstories.cms"},
    {"name": "The Hindu",         "category": "India",    "url": "https://www.thehindu.com/news/national/feeder/default.rss"},
    {"name": "Hindustan Times",   "category": "India",    "url": "https://www.hindustantimes.com/feeds/rss/latest/rssfeed.xml"},
    {"name": "NDTV",              "category": "India",    "url": "https://feeds.feedburner.com/ndtvnews-top-stories"},
    # Tamil
    {"name": "Dinamalar",         "category": "Tamil",    "url": _google_news("dinamalar.com", lang="ta")},
    {"name": "Goodreturns Tamil", "category": "Tamil",    "url": _google_news("tamil.goodreturns.in", lang="ta")},
    # World
    {"name": "BBC",               "category": "World",    "url": "https://feeds.bbci.co.uk/news/rss.xml"},
    {"name": "CNN",               "category": "World",    "url": _google_news("cnn.com", lang="en-US", country="US")},
    {"name": "The Guardian",      "category": "World",    "url": "https://www.theguardian.com/world/rss"},
    {"name": "Guardian US",       "category": "World",    "url": "https://www.theguardian.com/us/rss"},
    {"name": "Al Jazeera",        "category": "World",    "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "New York Times",    "category": "World",    "url": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"},
    {"name": "TIME",              "category": "World",    "url": "https://time.com/feed/"},
    # Business
    {"name": "Forbes",            "category": "Business", "url": "https://www.forbes.com/business/feed/"},
    {"name": "Forbes India",      "category": "Business", "url": _google_news("forbesindia.com")},
    {"name": "Wall Street Journal", "category": "Business", "url": _google_news("wsj.com", lang="en-US", country="US")},
    {"name": "BBC Business",      "category": "Business", "url": "https://feeds.bbci.co.uk/news/business/rss.xml"},
    {"name": "CNBC",              "category": "Business", "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html"},
    {"name": "Economic Times",    "category": "Business", "url": "https://economictimes.indiatimes.com/rssfeedstopstories.cms"},
    {"name": "Business Standard", "category": "Business", "url": "https://www.business-standard.com/rss/latest.rss"},
    {"name": "Mint",              "category": "Business", "url": "https://www.livemint.com/rss/news"},
    {"name": "Entrepreneur",      "category": "Business", "url": "https://www.entrepreneur.com/latest.rss"},
    {"name": "YourStory",         "category": "Business", "url": "https://yourstory.com/feed"},
    # Tech — developer community, startups, AI, gadgets & engineering
    # (summary: False where the feed's description is just "Comments" / link noise)
    {"name": "Hacker News",       "category": "Tech",     "url": "https://hnrss.org/frontpage", "summary": False},
    {"name": "Lobsters",          "category": "Tech",     "url": "https://lobste.rs/rss", "summary": False},
    {"name": "DEV Community",     "category": "Tech",     "url": "https://dev.to/feed"},
    {"name": "GitHub Blog",       "category": "Tech",     "url": "https://github.blog/feed/"},
    {"name": "InfoQ",             "category": "Tech",     "url": "https://feed.infoq.com/"},
    {"name": "Software Engineering Daily", "category": "Tech", "url": "https://softwareengineeringdaily.com/feed/"},
    {"name": "Simon Willison",    "category": "Tech",     "url": "https://simonwillison.net/atom/everything/"},
    {"name": "Towards Data Science", "category": "Tech",  "url": "https://towardsdatascience.com/feed/"},
    {"name": "Hugging Face",      "category": "Tech",     "url": "https://huggingface.co/blog/feed.xml"},
    {"name": "OpenAI",            "category": "Tech",     "url": "https://openai.com/news/rss.xml"},
    {"name": "Google AI",         "category": "Tech",     "url": "https://blog.google/technology/ai/rss/"},
    {"name": "AI News",           "category": "Tech",     "url": _google_news("artificialintelligence-news.com", lang="en-US", country="US")},
    {"name": "Independent AI",    "category": "Tech",     "url": "https://www.independent.co.uk/topic/artificial-intelligence/rss"},
    {"name": "TechCrunch",        "category": "Tech",     "url": "https://techcrunch.com/feed/"},
    {"name": "The Verge",         "category": "Tech",     "url": "https://www.theverge.com/rss/index.xml"},
    {"name": "Ars Technica",      "category": "Tech",     "url": "https://feeds.arstechnica.com/arstechnica/index"},
    {"name": "Wired",             "category": "Tech",     "url": "https://www.wired.com/feed/rss"},
    {"name": "Engadget",          "category": "Tech",     "url": "https://www.engadget.com/rss.xml"},
    {"name": "GeekWire",          "category": "Tech",     "url": "https://www.geekwire.com/feed/"},
    {"name": "SiliconANGLE",      "category": "Tech",     "url": "https://siliconangle.com/feed/"},
    {"name": "TechRepublic",      "category": "Tech",     "url": "https://www.techrepublic.com/rssfeeds/articles/"},
    {"name": "WSJ Tech",          "category": "Tech",     "url": _google_news("wsj.com/tech", lang="en-US", country="US")},
    {"name": "The Hacker News",   "category": "Tech",     "url": "https://feeds.feedburner.com/TheHackersNews"},
    {"name": "Product Hunt",      "category": "Tech",     "url": "https://www.producthunt.com/feed"},
    {"name": "BetaList",          "category": "Tech",     "url": "https://feeds.feedburner.com/BetaList"},
    {"name": "Hackaday",          "category": "Tech",     "url": "https://hackaday.com/blog/feed/"},
    {"name": "Make",              "category": "Tech",     "url": "https://makezine.com/feed/"},
    {"name": "Tech Xplore",       "category": "Tech",     "url": "https://techxplore.com/rss-feed/"},
    {"name": "PCWorld",           "category": "Tech",     "url": "https://www.pcworld.com/feed"},
    {"name": "What Hi-Fi",        "category": "Tech",     "url": "https://www.whathifi.com/feeds/all"},
    {"name": "ENR",               "category": "Tech",     "url": _google_news("enr.com", lang="en-US", country="US")},
    {"name": "Darius Foroux",     "category": "Tech",     "url": "https://dariusforoux.com/feed/"},
    # Sports
    {"name": "BBC Sport",         "category": "Sports",   "url": "https://feeds.bbci.co.uk/sport/rss.xml"},
    {"name": "ESPNcricinfo",      "category": "Sports",   "url": "https://www.espncricinfo.com/rss/content/story/feeds/0.xml"},
    {"name": "Sportstar",         "category": "Sports",   "url": "https://sportstar.thehindu.com/feeder/default.rss"},
    {"name": "TOI Sports",        "category": "Sports",   "url": "https://timesofindia.indiatimes.com/rssfeeds/4719148.cms"},
    {"name": "NDTV Sports",       "category": "Sports",   "url": "https://feeds.feedburner.com/ndtvsports-latest"},
    # Triathlon & open-water swimming
    {"name": "220 Triathlon",     "category": "Triathlon", "url": "https://www.220triathlon.com/feed/"},
    {"name": "Triathlete",        "category": "Triathlon", "url": "https://www.triathlete.com/feed/"},
    {"name": "Slowtwitch",        "category": "Triathlon", "url": "https://www.slowtwitch.com/rss"},
    {"name": "IRONMAN news",      "category": "Triathlon", "url": _google_news_search('"IRONMAN" triathlon', "en-US", "US"), "must_match": r"\b(iron ?man|70\.3|triathl(on|ete|etes))\b"},
    {"name": "Triathlon India",   "category": "Triathlon", "url": _google_news_search('"triathlon" India'), "must_match": r"\b(triathl(on|ete|etes)|iron ?man|70\.3|aquathlon|duathlon)\b"},
    {"name": "Open water swimming", "category": "Triathlon", "url": _google_news_search('"open water swim" OR "swimathon"'), "must_match": r"\b(swim(mer|mers|ming|athon)?|open[- ]water|channel crossing|aquathlon)\b"},
    # Running — domestic and international
    {"name": "Runner's World",    "category": "Running",  "url": "https://www.runnersworld.com/rss/all.xml"},
    {"name": "LetsRun",           "category": "Running",  "url": "https://www.letsrun.com/feed/"},
    {"name": "iRunFar",           "category": "Running",  "url": "https://www.irunfar.com/feed"},
    {"name": "Marathons India",   "category": "Running",  "url": _google_news_search('"marathon" India'), "must_match": r"\b(marathon|half[- ]marathon|ultra(marathon)?|runathon|\d+ ?k (run|race)|runners?|road race|trail race)\b",
     "must_not": r"\b(enters? (a )?marathon|marathon (session|sessions|meeting|talks|hearing|protest|innings|knock|negotiations|sitting|debate|interrogation|questioning|spell)|the marathon man)\b"},
    {"name": "Trail running",     "category": "Running",  "url": _google_news_search('"trail running" OR "UTMB" OR "ultramarathon"', "en-US", "US"), "must_match": r"\b(trail|ultra(marathon|trail)?|utmb|run(ner|ners|ning)?|skyrace)\b"},
    # Hackathons
    {"name": "MLH",               "category": "Hackathon", "url": "https://news.mlh.io/feed"},
    {"name": "Hackathons India",  "category": "Hackathon", "url": _google_news_search('"hackathon" India'), "must_match": r"\b(hack(athon|athons|fest|ers?)|ideathon|buildathon|codathon|datathon|devfolio|devpost|mlh|coding (contest|challenge|competition))\b"},
    {"name": "Hackathons",        "category": "Hackathon", "url": _google_news_search('"hackathon"', "en-US", "US"), "must_match": r"\b(hack(athon|athons|fest|ers?)|ideathon|buildathon|codathon|datathon|devfolio|devpost|mlh|coding (contest|challenge|competition))\b"},
    # Markets
    {"name": "Bloomberg",         "category": "Markets",  "url": "https://feeds.bloomberg.com/markets/news.rss"},
    {"name": "Moneycontrol",      "category": "Markets",  "url": _google_news("moneycontrol.com")},
    {"name": "Investing.com",     "category": "Markets",  "url": "https://www.investing.com/rss/news.rss"},
    {"name": "Investopedia",      "category": "Markets",  "url": _google_news("investopedia.com", lang="en-US", country="US")},
    {"name": "TradingView",       "category": "Markets",  "url": _google_news("tradingview.com/news")},
    # Markets & economy — regulators, Indian market desks, global macro
    {"name": "RBI",               "category": "Markets",  "url": "https://www.rbi.org.in/pressreleases_rss.xml"},
    {"name": "ET Markets",        "category": "Markets",  "url": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"},
    {"name": "Mint Markets",      "category": "Markets",  "url": "https://www.livemint.com/rss/markets"},
    {"name": "BS Markets",        "category": "Markets",  "url": "https://www.business-standard.com/rss/markets-106.rss"},
    {"name": "CNBC TV18 Markets", "category": "Markets",  "url": "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml"},
    {"name": "Moneycontrol Markets", "category": "Markets", "url": _google_news("moneycontrol.com/news/business/markets")},
    {"name": "Sensex & Nifty",    "category": "Markets",  "url": _google_news_search('"Sensex" OR "Nifty"'), "must_match": r"\b(sensex|nifty|bse|nse|stocks?|shares|market|indices|equit(y|ies))\b"},
    {"name": "CNBC Markets",      "category": "Markets",  "url": "https://www.cnbc.com/id/15839069/device/rss/rss.html"},
    {"name": "CNBC Economy",      "category": "Markets",  "url": "https://www.cnbc.com/id/20910258/device/rss/rss.html"},
    {"name": "MarketWatch",       "category": "Markets",  "url": "https://feeds.marketwatch.com/marketwatch/topstories/"},
    {"name": "Financial Times",   "category": "Markets",  "url": "https://www.ft.com/markets?format=rss"},
    {"name": "US jobs & Fed",     "category": "Markets",  "url": _google_news_search('"jobs report" OR payrolls OR "Federal Reserve"', "en-US", "US"), "must_match": r"\b(jobs?|payrolls?|unemployment|labou?r|fed|federal reserve|fomc|powell|rate (cut|hike)s?|inflation)\b"},
    {"name": "Japan markets",     "category": "Markets",  "url": _google_news_search('Nikkei OR "Bank of Japan" OR yen', "en-US", "US"), "must_match": r"\b(nikkei|topix|yen|boj|bank of japan|japan(ese)? (stocks?|shares|market|economy|bonds?|yields?))\b"},
    {"name": "China markets",     "category": "Markets",  "url": _google_news_search('"China" (stocks OR yuan OR economy OR PBOC)', "en-US", "US"), "must_match": r"\b(china|chinese|yuan|renminbi|pboc|shanghai|shenzhen|hang seng|beijing)\b"},
]

# Tabs on the News page. A tab shows one or more source categories; tabs with
# events=True show an upcoming-events timeline (src/news_events.py) on top.
# "All" is always shown first and isn't listed here.
NEWS_TABS = [
    {"key": "India",     "categories": ["India"]},
    {"key": "World",     "categories": ["World"]},
    {"key": "Finance",   "categories": ["Business", "Markets"], "events": True},
    {"key": "Business",  "categories": ["Business"]},
    {"key": "Markets",   "categories": ["Markets"]},
    {"key": "Tech",      "categories": ["Tech"]},
    {"key": "Hackathon", "categories": ["Hackathon"], "events": True},
    {"key": "Sports",    "categories": ["Sports"], "events": True},
    {"key": "Running",   "categories": ["Running"], "events": True},
    {"key": "Triathlon", "categories": ["Triathlon"], "events": True},
    {"key": "Tamil",     "categories": ["Tamil"]},
]
TAB_KEYS = [t["key"] for t in NEWS_TABS]

# Domestic vs international (event tabs' filter checkboxes). An article is
# India-related if it mentions India / an Indian city or state, or comes from an
# India-specific feed.
INDIA_HINTS = re.compile(r"\b(india|indian|indians|bharat|bengaluru|bangalore|mumbai|delhi|chennai|hyderabad|pune|kolkata|ahmedabad|kochi|cochin|goa|"
    r"jaipur|lucknow|chandigarh|mysuru|mysore|udupi|mangaluru|mangalore|nashik|nagpur|indore|bhopal|surat|vadodara|noida|gurugram|"
    r"gurgaon|thiruvananthapuram|trivandrum|coimbatore|madurai|visakhapatnam|vizag|bhubaneswar|guwahati|dehradun|manali|"
    r"karnataka|maharashtra|tamil nadu|kerala|telangana|andhra pradesh|gujarat|rajasthan|uttar pradesh|uttarakhand|"
    r"himachal|haryana|punjab|west bengal|odisha|assam|madhya pradesh|bihar|jharkhand|chhattisgarh|sikkim|meghalaya|ladakh|"
    r"jammu|kashmir|puducherry|pondicherry)\b", re.I)
INDIA_FOCUSED_SOURCES = {"Triathlon India", "Marathons India", "Hackathons India", "Dinamalar", "Goodreturns Tamil"}


# Finance tab ranking: how much a story is about markets / macro / money.
# Weighted terms add up (capped at 100); off-topic terms subtract.
_FINANCE_TERMS = [
    (35, r"rbi|repo rate|monetary policy|mpc|rate (cut|hike|decision)|interest rates?|fed\b|fomc|powell|central bank|ecb|bank of japan|boj|pboc"),
    (30, r"inflation|cpi|wpi|gdp|payrolls?|jobs report|jobless|unemployment|labou?r market|recession|fiscal deficit|budget"),
    (30, r"sensex|nifty|stock markets?|stocks?|shares|equit(y|ies)|bond yields?|treasur(y|ies)|wall street|dow|nasdaq|s&p|nikkei|hang seng|shanghai|markets? (rally|fall|crash)"),
    (25, r"ipo|buyback|dividend|bonus issue|stock split|earnings|quarterly results|q[1-4] results|profit|revenue|guidance|listing"),
    (25, r"rupee|dollar|yen|yuan|forex|currency|crude|oil prices?|gold prices?|commodit(y|ies)|tariffs?|trade (deal|war)|exports?|imports?"),
    (20, r"sebi|fiis?|fpis?|diis?|mutual funds?|sips?|investors?|brokerages?|target price|ratings?|downgrades?|upgrades?|valuations?"),
    (12, r"banks?|economy|economic|market|funding|merger|acquisition|deal|china|japan|us economy"),
]
_FINANCE_OFFTOPIC = r"movies?|films?|box office|bollywood|actor|actress|celebrity|cricket|football|election|polls?|voters?|wedding|murder|crime|weather"
_FINANCE_RES = [(w, re.compile(rf"\b({p})\b", re.I)) for w, p in _FINANCE_TERMS]
_FINANCE_OFF_RE = re.compile(rf"\b({_FINANCE_OFFTOPIC})\b", re.I)


def finance_relevance(item: dict) -> int:
    text = f"{item.get('title', '')} {item.get('summary') or ''}"
    score = sum(w for w, rx in _FINANCE_RES if rx.search(text))
    if _FINANCE_OFF_RE.search(item.get("title", "")):
        score -= 40
    return max(0, min(100, score))


def article_country(item: dict) -> str | None:
    """"India" for India-related articles, else None (treated as international)."""
    if item.get("source") in INDIA_FOCUSED_SOURCES or INDIA_HINTS.search(f"{item.get('title', '')} {item.get('summary') or ''}"):
        return "India"
    return None
# Profiles saved before tabs were configurable keep the tabs they had
LEGACY_TABS = ["India", "World", "Business", "Tech", "Markets", "Tamil"]

_NS = {
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
}

_TAG_RE = re.compile(r"<[^>]+>")
_IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)
_WS_RE = re.compile(r"\s+")

TIER1_PUBLISHERS = {
    "BBC", "Bloomberg", "Wall Street Journal", "New York Times", "The Guardian",
    "CNBC", "Economic Times", "Moneycontrol", "Mint", "Business Standard",
    "The Hindu", "Times of India", "Forbes",
    "TechCrunch", "The Verge", "Ars Technica", "Wired", "Hacker News",
}

# Feeds that are sections of the same publisher shouldn't count as
# independent coverage of a story
_PUBLISHER_ALIASES = {"BBC Business": "BBC", "Guardian US": "The Guardian"}

IMPORTANT_KEYWORDS = [
    "breaking", "live", "rbi", "sebi", "repo rate", "rate cut", "rate hike", "fed",
    "federal reserve", "inflation", "gdp", "recession", "budget", "tariff", "sanction",
    "sensex", "nifty", "stock market", "crash", "plunge", "surge", "record high",
    "rupee", "crude", "oil price", "gold price", "ipo", "merger", "acquisition",
    "earnings", "results", "election", "war", "ceasefire", "attack", "earthquake",
    "cyclone", "flood", "killed", "prime minister", "president", "supreme court",
    "open source", "vulnerability", "zero-day", "data breach", "outage", "llm",
]
_KEYWORD_RES = [(k, re.compile(r"\b" + re.escape(k) + r"\b")) for k in IMPORTANT_KEYWORDS]

_STOPWORDS = set("""
the a an and or but of to in on at for from by with as is are was were be been has have had
will would could should may might can its it this that these those after before over under
into out up down about than then new says said amid via not no more most how why what who
when where which while your you our their they his her him she he we us all just
""".split())
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

_refresh_lock = asyncio.Lock()


def _clean_text(raw: str) -> str:
    if not raw:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", html.unescape(raw)))
    return _WS_RE.sub(" ", text).strip()


def _parse_date(raw: str):
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


_ATOM = "{http://www.w3.org/2005/Atom}"
MAX_SUMMARY_CHARS = 600        # some blogs put the whole article in the feed


def _entry_fields(el, is_atom: bool):
    """(title, link, description_html, published_raw) for an RSS <item> or Atom <entry>."""
    if not is_atom:
        return (el.findtext("title", ""), (el.findtext("link") or "").strip(),
                el.findtext("description", "") or "",
                el.findtext("pubDate") or el.findtext("{http://purl.org/dc/elements/1.1/}date"))
    link = ""
    for l in el.findall(_ATOM + "link"):
        if l.get("rel", "alternate") == "alternate" and l.get("href"):
            link = l.get("href").strip()
            break
    description = el.findtext(_ATOM + "summary") or el.findtext(_ATOM + "content") or ""
    return (el.findtext(_ATOM + "title", ""), link, description,
            el.findtext(_ATOM + "published") or el.findtext(_ATOM + "updated"))


def _find_image(item, description_raw: str):
    for tag in ("media:content", "media:thumbnail"):
        for el in item.findall(tag, _NS):
            url = el.get("url")
            if url and el.get("medium", "image") == "image":
                return url
    enclosure = item.find("enclosure")
    if enclosure is not None and (enclosure.get("type") or "").startswith("image"):
        return enclosure.get("url")
    for raw in (description_raw, item.findtext("content:encoded", "", _NS)):
        m = _IMG_RE.search(html.unescape(raw or ""))
        if m:
            return m.group(1)
    return None


def _fetch_source(source: dict) -> list:
    resp = requests.get(source["url"], headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    is_google = "news.google.com" in source["url"]
    must = re.compile(source["must_match"], re.I) if source.get("must_match") else None
    must_not = re.compile(source["must_not"], re.I) if source.get("must_not") else None
    entries = list(root.iter("item"))
    is_atom = not entries
    if is_atom:
        entries = list(root.iter(_ATOM + "entry"))

    items = []
    for item in entries:
        title_raw, link, description_raw, published_raw = _entry_fields(item, is_atom)
        title = _clean_text(title_raw)
        if not title or not link:
            continue
        # Google News appends " - Publisher" to every title
        if is_google and " - " in title:
            title = title.rsplit(" - ", 1)[0].strip()

        summary = "" if is_google or not source.get("summary", True) else _clean_text(description_raw)
        if summary == title:
            summary = ""
        if len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[:MAX_SUMMARY_CHARS].rsplit(" ", 1)[0] + "…"
        published = _parse_date(published_raw)
        # Topic searches (Google News) also return pages that merely mention the term in a
        # sidebar — keep only articles whose own headline/summary is on topic
        if must and not must.search(f"{title} {summary}"):
            continue
        if must_not and must_not.search(f"{title} {summary}"):
            continue   # figurative use, e.g. "protest enters marathon 8th hour"

        items.append({
            "title": title,
            "summary": summary,
            "link": link,
            "image": None if is_google else _find_image(item, description_raw),
            "published": published.isoformat() if published else None,
            "source": source["name"],
            "category": source["category"],
        })
        if len(items) >= MAX_ITEMS_PER_FEED:
            break
    return items


RETRY_DELAY_SEC = 3            # failed sources get one more try after this pause
NETWORK_DOWN_FRACTION = 0.5    # more than this share failing → treat the network as down


class NetworkDownError(RuntimeError):
    """Most sources unreachable (e.g. Wi-Fi not back yet after sleep) — don't store this run."""


def _fetch_with_retry(source: dict) -> list:
    # Brief DNS / connection blips are common (especially right after wake) — retry once
    try:
        return _fetch_source(source)
    except Exception:
        time.sleep(RETRY_DELAY_SEC)
        return _fetch_source(source)


def _fetch_all(cutoff: datetime) -> dict:
    source_status = []
    all_items = []

    with ThreadPoolExecutor(max_workers=min(32, len(NEWS_SOURCES))) as pool:
        futures = {pool.submit(_fetch_with_retry, s): s for s in NEWS_SOURCES}
        for future, source in futures.items():
            try:
                items = future.result()
                fresh = [
                    i for i in items
                    if not i["published"] or datetime.fromisoformat(i["published"]) >= cutoff
                ]
                all_items.extend(fresh)
                source_status.append({"name": source["name"], "category": source["category"], "count": len(fresh), "ok": True})
            except Exception as e:
                print(f"[news] {source['name']} failed: {e}")
                source_status.append({"name": source["name"], "category": source["category"], "count": 0, "ok": False, "error": str(e)[:200]})

    failed = sum(not st["ok"] for st in source_status)
    if failed > len(source_status) * NETWORK_DOWN_FRACTION:
        raise NetworkDownError(f"network looks down — {failed} of {len(source_status)} sources unreachable; will retry shortly")
    return {"items": all_items, "sources": source_status}


# ── Ranking ──────────────────────────────────────────────────────────────

def _title_key(title: str) -> str:
    return re.sub(r"\W+", "", title.lower())[:80]


def _title_tokens(title: str) -> set:
    return {t for t in _TOKEN_RE.findall(title.lower()) if len(t) >= 3 and t not in _STOPWORDS}


def _publisher(source: str) -> str:
    return _PUBLISHER_ALIASES.get(source, source)


def _score_items(items: list, now: datetime) -> None:
    """Attach base_score, score_breakdown, related_sources, keyword_hits and story_id to each item in place."""
    tokens = [_title_tokens(i["title"]) for i in items]
    postings = defaultdict(list)
    for idx, toks in enumerate(tokens):
        for t in toks:
            postings[t].append(idx)

    parent = list(range(len(items)))   # union-find over "same story" links

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for idx, item in enumerate(items):
        # Same story elsewhere = headlines sharing ≥3 significant words that
        # make up at least half of the shorter headline
        shared = Counter()
        for t in tokens[idx]:
            plist = postings[t]
            if len(plist) > 80:        # too common to identify a story
                continue
            for j in plist:
                if j != idx:
                    shared[j] += 1
        own = _publisher(item["source"])
        related = set()
        for j, n in shared.items():
            other = _publisher(items[j]["source"])
            if other != own and n >= 3 and n / min(len(tokens[idx]), len(tokens[j])) >= 0.5:
                related.add(other)
                parent[find(j)] = find(idx)

        title_lc = item["title"].lower()
        hits = [k for k, rx in _KEYWORD_RES if rx.search(title_lc)]

        age_h = max(0.0, (now - item["published_at"]).total_seconds() / 3600)
        breakdown = {
            "recency": 100 * 0.5 ** (age_h / RECENCY_HALF_LIFE_HOURS),
            "coverage": min(len(related), 5) / 5 * 100,
            "keywords": min(len(hits), 2) / 2 * 100,
            "source": 100 if own in TIER1_PUBLISHERS else 60,
        }
        item["base_score"] = round(
            0.45 * breakdown["recency"] + 0.30 * breakdown["coverage"]
            + 0.15 * breakdown["keywords"] + 0.10 * breakdown["source"], 1)
        item["score_breakdown"] = {k: round(v, 1) for k, v in breakdown.items()}
        item["related_sources"] = sorted(related)
        item["keyword_hits"] = hits
        item["scored_at"] = now

    # A story is identified by its highest base-scoring article
    clusters = defaultdict(list)
    for idx in range(len(items)):
        clusters[find(idx)].append(items[idx])
    for members in clusters.values():
        story_id = max(members, key=lambda i: i["base_score"])["link"]
        for m in members:
            m["story_id"] = story_id
            m["story_size"] = len(members)


# ── Personalisation ──────────────────────────────────────────────────────

INTEREST_WEIGHT = 0.4          # share of the final score driven by the user's interests
PREFERENCE_USER_ID = "Kamal"   # single-user app — matches the rest of the routes

DEFAULT_PREFERENCES = {
    "topics": [],              # followed topics / keywords, e.g. "Nifty", "RBI", "AI"
    "preferred_sources": [],
    "muted_sources": [],       # never shown
    "muted_keywords": [],      # stories mentioning these are never shown
    "retention_days": 2,       # keep news this long, then clear it
    "refresh_minutes": 10,     # background refresh interval
    "sort_mode": "top",        # News page toggle: "top" (Rank) or "latest"
    "tabs": [],                # tabs shown after "All", in order (empty → only "All")
    "custom_tabs": [],         # user-defined tabs: [{"name": "Chess", "keywords": ["chess", "Gukesh"]}]
    "event_filters": {},       # per events tab: {"Running": {"domestic": true, "international": false, ...}}
}
EVENT_FILTER_KEYS = ("local", "national", "international", "online", "in_person")
MAX_CUSTOM_TABS = 10
MAX_TAB_KEYWORDS = 10
SORT_MODES = ("top", "latest")
_SETTING_OPTIONS = {"retention_days": RETENTION_OPTIONS, "refresh_minutes": REFRESH_OPTIONS}

SUGGESTED_TOPICS = [
    "Nifty", "Sensex", "RBI", "SEBI", "Mutual funds", "IPO", "Gold", "Crude oil", "Rupee",
    "US Fed", "Inflation", "Budget", "Startups", "AI", "Tamil Nadu", "Chennai", "Bengaluru",
    "Real estate", "Cricket", "Elections", "LLM", "Open source", "Cybersecurity", "Python",
]


def normalise_preferences(raw) -> dict:
    """Coerce stored/posted preferences into DEFAULT_PREFERENCES' shape with de-duplicated, trimmed lists."""
    raw = raw or {}
    prefs = {}
    for key, options in _SETTING_OPTIONS.items():
        try:
            value = int(raw.get(key))
        except (TypeError, ValueError):
            value = None
        prefs[key] = value if value in options else DEFAULT_PREFERENCES[key]
    prefs["sort_mode"] = raw.get("sort_mode") if raw.get("sort_mode") in SORT_MODES else DEFAULT_PREFERENCES["sort_mode"]
    # Custom tabs: unique names (not clashing with built-in tabs), each with 1+ keywords
    custom, taken = [], {k.lower() for k in TAB_KEYS} | {"all"}
    for ct in raw.get("custom_tabs") or []:
        if not isinstance(ct, dict):
            continue
        name = str(ct.get("name") or "").strip()[:24]
        if not name or name.lower() in taken:
            continue
        words = [str(w).strip() for w in (ct.get("keywords") or []) if str(w).strip()]
        words = list(dict.fromkeys(words))[:MAX_TAB_KEYWORDS] or [name]
        taken.add(name.lower())
        custom.append({"name": name, "keywords": words})
    prefs["custom_tabs"] = custom[:MAX_CUSTOM_TABS]

    raw_filters = raw.get("event_filters") if isinstance(raw.get("event_filters"), dict) else {}
    prefs["event_filters"] = {}
    for t in NEWS_TABS:
        if not t.get("events"):
            continue
        saved = dict(raw_filters.get(t["key"]) or {})
        if "domestic" in saved:   # before Local / National existed, "domestic" covered both
            saved.setdefault("local", saved["domestic"])
            saved.setdefault("national", saved["domestic"])
        prefs["event_filters"][t["key"]] = {k: bool(saved.get(k, True)) for k in EVENT_FILTER_KEYS}

    valid = set(TAB_KEYS) | {c["name"] for c in prefs["custom_tabs"]}
    tabs = raw["tabs"] if isinstance(raw.get("tabs"), list) else LEGACY_TABS
    prefs["tabs"] = list(dict.fromkeys(t for t in tabs if t in valid))
    for key in DEFAULT_PREFERENCES:
        if key in _SETTING_OPTIONS or key in ("sort_mode", "tabs", "custom_tabs", "event_filters"):
            continue
        seen, values = set(), []
        for v in raw.get(key) or []:
            v = str(v).strip()
            if v and v.lower() not in seen:
                seen.add(v.lower())
                values.append(v)
        prefs[key] = values
    return prefs


def has_interests(prefs: dict) -> bool:
    return bool(prefs["topics"] or prefs["preferred_sources"])


def _term_regex(term: str):
    t = re.escape(term.lower())
    # \b is unreliable around Indic combining vowel signs — plain substring there
    return re.compile(rf"\b{t}\b") if term.isascii() else re.compile(t)


def _apply_preferences(items: list, prefs: dict) -> None:
    """Set interest, matched_topics, muted and the personalised score; then pick each story's lead."""
    topics = [(t, _term_regex(t)) for t in prefs["topics"]]
    muted_kw = [_term_regex(t) for t in prefs["muted_keywords"]]
    preferred = set(prefs["preferred_sources"])
    muted_sources = set(prefs["muted_sources"])
    personalised = has_interests(prefs)

    for item in items:
        text = (item["title"] + " " + (item.get("summary") or "")).lower()
        matched = [t for t, rx in topics if rx.search(text)]
        interest = (80 if len(matched) > 1 else 60 if matched else 0)
        interest += 20 if item["source"] in preferred else 0
        interest = min(100, interest)

        item["matched_topics"] = matched
        item["interest"] = interest
        item["score_breakdown"]["interest"] = interest
        item["muted"] = item["source"] in muted_sources or any(rx.search(text) for rx in muted_kw)
        item["score"] = (
            round((1 - INTEREST_WEIGHT) * item["base_score"] + INTEREST_WEIGHT * interest, 1)
            if personalised else item["base_score"]
        )

    # The Top view shows one article per story — its best-scoring visible one
    stories = defaultdict(list)
    for item in items:
        stories[item["story_id"]].append(item)
    for members in stories.values():
        visible = [m for m in members if not m["muted"]]
        lead = max(visible, key=lambda i: i["score"]) if visible else None
        for m in members:
            m["is_lead"] = m is lead


async def get_preferences(db) -> dict:
    profile = await db.user_profiles.find_one({"user_id": PREFERENCE_USER_ID}, {"news_preferences": 1})
    return normalise_preferences((profile or {}).get("news_preferences"))


async def save_preferences(db, raw: dict) -> dict:
    """Store preferences on the user profile and immediately rescore stored news with them."""
    prefs = normalise_preferences(raw)
    await db.user_profiles.update_one(
        {"user_id": PREFERENCE_USER_ID}, {"$set": {"news_preferences": prefs}}, upsert=True
    )
    async with _refresh_lock:
        await _rank_and_store(db, await _load_window(db, prefs), datetime.now(timezone.utc), prefs)
    return prefs


async def save_event_filters(db, tab: str, filters: dict) -> dict:
    """Persist one events tab's Domestic / International / Online / In-person checkboxes."""
    if not any(t["key"] == tab and t.get("events") for t in NEWS_TABS):
        raise ValueError(f"{tab} has no events timeline")
    clean = {k: bool(filters.get(k, True)) for k in EVENT_FILTER_KEYS}
    await db.user_profiles.update_one(
        {"user_id": PREFERENCE_USER_ID}, {"$set": {f"news_preferences.event_filters.{tab}": clean}}, upsert=True
    )
    return clean


async def save_sort_mode(db, mode: str) -> str:
    """Persist the Rank / Latest toggle — display only, so no rescoring needed."""
    if mode not in SORT_MODES:
        raise ValueError(f"sort_mode must be one of {SORT_MODES}")
    await db.user_profiles.update_one(
        {"user_id": PREFERENCE_USER_ID}, {"$set": {"news_preferences.sort_mode": mode}}, upsert=True
    )
    return mode


# ── Saved (liked) stories ────────────────────────────────────────────────

_SAVED_FIELDS = ("title", "summary", "link", "image", "source", "category", "published_at",
                 "story_id", "score", "base_score", "interest", "matched_topics", "related_sources")


async def save_story(db, link: str) -> dict:
    """Copy a story into news_saved (idempotent)."""
    doc = await db.news_items.find_one({"link": link}, {"_id": 0})
    if not doc:
        raise LookupError("Story not found — it may have been cleared; refresh the page")
    saved = {k: doc[k] for k in _SAVED_FIELDS if k in doc}
    saved.update({"user_id": PREFERENCE_USER_ID, "liked_at": datetime.now(timezone.utc)})
    await db.news_saved.update_one({"link": link}, {"$setOnInsert": saved}, upsert=True)
    return saved


async def unsave_story(db, link: str) -> None:
    await db.news_saved.delete_one({"link": link})


async def list_saved(db) -> list:
    docs = await db.news_saved.find({"user_id": PREFERENCE_USER_ID}, {"_id": 0}).sort("liked_at", -1).to_list(None)
    for d in docs:
        if d.get("published_at"):
            d["published"] = _as_utc(d.pop("published_at")).isoformat()
        d["liked_at"] = _as_utc(d["liked_at"]).isoformat()
    return docs


# ── Upcoming events (written by src/news_events.py) ─────────────────────

async def _home(db) -> dict:
    """User's home city/country for "domestic" event filtering (see news_events.home_location)."""
    from .news_events import home_location
    profile = await db.user_profiles.find_one({"user_id": PREFERENCE_USER_ID}, {"city": 1, "timezone": 1})
    cities = await db.country_cities.find({}, {"country_key": 1, "cities.city": 1}).to_list(None)
    return home_location(profile, cities)

async def upcoming_events(db) -> dict:
    """{tab: [events from today on, soonest first]}"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = defaultdict(list)
    async for e in db.news_events.find({"end_date": {"$gte": today}}, {"_id": 0}).sort([("start_date", 1), ("name", 1)]):
        for key in ("extracted_at",):
            if e.get(key):
                e[key] = _as_utc(e[key]).isoformat()
        out[e["tab"]].append(e)
    return dict(out)


# ── Persistence ──────────────────────────────────────────────────────────

def _as_utc(dt: datetime) -> datetime:
    # Motor returns naive datetimes (stored as UTC)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _cutoff(prefs: dict) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=prefs["retention_days"])


async def _load_window(db, prefs: dict) -> list:
    docs = await db.news_items.find({"published_at": {"$gte": _cutoff(prefs)}}, {"_id": 0}).to_list(None)
    for doc in docs:
        doc["published_at"] = _as_utc(doc["published_at"])
    return docs


async def _rank_and_store(db, items: list, now: datetime, prefs: dict) -> None:
    """Score items (general + personal), upsert them and clear news past the retention period."""
    await db.news_items.delete_many({"published_at": {"$lt": _cutoff(prefs)}})
    if not items:
        return
    _score_items(items, now)
    _apply_preferences(items, prefs)
    await db.news_items.bulk_write(
        [ReplaceOne({"link": i["link"]}, i, upsert=True) for i in items], ordered=False
    )


async def refresh_news(db) -> dict:
    """Fetch all feeds, merge with stored items in the ranking window, rescore everything and upsert."""
    prefs = await get_preferences(db)
    fetched = await asyncio.to_thread(_fetch_all, _cutoff(prefs))
    now = datetime.now(timezone.utc)
    # Mongo stores milliseconds — truncate so every reader sees the same fetched_at
    now = now.replace(microsecond=now.microsecond // 1000 * 1000)

    # Stored items stay in the ranking pool (and count toward coverage) even
    # after they drop off their feed; recency is recomputed on every refresh
    merged = {_title_key(doc["title"]): doc for doc in await _load_window(db, prefs)}

    seen_links = {d["link"] for d in merged.values()}
    new_count = 0
    for item in fetched["items"]:
        key = _title_key(item["title"])
        published = item.pop("published")
        if key not in merged and item["link"] in seen_links:
            continue    # link is the unique key — same article re-titled
        if key in merged:
            existing = merged[key]
            existing["last_seen"] = now
            for field in ("summary", "image"):
                if item.get(field) and not existing.get(field):
                    existing[field] = item[field]
            continue
        item["published_at"] = datetime.fromisoformat(published) if published else now
        item["first_seen"] = now
        item["last_seen"] = now
        merged[key] = item
        seen_links.add(item["link"])
        new_count += 1

    items = list(merged.values())
    await _rank_and_store(db, items, now, prefs)

    meta = {
        "fetched_at": now, "sources": fetched["sources"], "item_count": len(items), "new_count": new_count,
        "sources_failed": [s["name"] for s in fetched["sources"] if not s["ok"]],
    }
    await db.news_meta.replace_one({"_id": "last_refresh"}, meta, upsert=True)
    return meta


async def get_ranked_news(db) -> dict:
    """Read-only: visible news in the retention window, sorted by personalised score.

    Never fetches — refreshing is Celery's job (src/news_refresh.py)."""
    meta = await db.news_meta.find_one({"_id": "last_refresh"}) or {}
    prefs = await get_preferences(db)
    docs = await db.news_items.find(
        {"published_at": {"$gte": _cutoff(prefs)}, "muted": {"$ne": True}}, {"_id": 0}
    ).sort([("score", -1), ("published_at", -1)]).to_list(None)

    # An image used by several different articles is a site placeholder/logo, not a photo
    image_use = Counter(d.get("image") for d in docs if d.get("image"))
    items = []
    for d in docs:
        if d.get("image") and image_use[d["image"]] >= 4:
            d["image"] = None
        d["country"] = article_country(d)
        if d.get("category") in ("Business", "Markets"):
            # Finance tab sorts by this: general score blended with market relevance
            d["finance_relevance"] = finance_relevance(d)
            d["finance_score"] = round(0.5 * d["score"] + 0.5 * d["finance_relevance"], 1)
        d["published"] = _as_utc(d.pop("published_at")).isoformat()
        for field in ("first_seen", "last_seen", "scored_at"):
            if d.get(field):
                d[field] = _as_utc(d[field]).isoformat()
        items.append(d)

    return {
        "items": items,
        "sources": meta.get("sources", []),
        "fetched_at": _as_utc(meta["fetched_at"]).isoformat() if meta.get("fetched_at") else None,
        "preferences": prefs,
        "personalised": has_interests(prefs),
        "saved_links": await db.news_saved.distinct("link", {"user_id": PREFERENCE_USER_ID}),
        "tabs_catalog": NEWS_TABS,
        "events": await upcoming_events(db),
        "home": await _home(db),
    }
