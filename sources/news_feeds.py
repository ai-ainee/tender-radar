"""sources/news_feeds.py — verified business-news RSS feeds.

Feeds split into two tiers:
  Tier 1 (verified in run 5): ET, BS, Mint, Hindu BusinessLine, Inc42
  Tier 2 (best-guess replacements for dead feeds): Construction World,
          NBM&CW, Infrastructure Today, Moneycontrol Business

Tier 2 URLs may need adjustment if the outlet changes their feed path.
If a Tier 2 feed returns 0 items for 3 consecutive runs, replace its URL.
"""
import re
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from common import log

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

FEEDS = {
    # ── Tier 1: verified working ──────────────────────────────────
    "et":                   "https://economictimes.indiatimes.com/rssfeedstopstories.cms",
    "bs":                   "https://www.business-standard.com/rss/home_page_top_stories.rss",
    "mint":                 "https://www.livemint.com/rss/news",
    "hindu_bl":             "https://www.thehindubusinessline.com/feeder/default.rss",
    "inc42":                "https://inc42.com/feed/",
    # ── Tier 2: best-guess replacements for dead feeds ────────────
    "moneycontrol_business":"https://www.moneycontrol.com/rss/business.xml",
    "construction_world":   "https://www.constructionworld.in/rss/latest",
    "nbmcw":                "https://www.nbmcw.com/rss.xml",
    "infrastructure_today": "https://www.infrastructuretoday.co.in/rss",
}

# Intent-based filter: keep only procurement-related news
INTENT = re.compile(
    r"\b(tender|procurement|expansion|capex|facility|plant|contract|"
    r"order|project|hiring|investment|acquisition|partnership|"
    r"commission|launch|wins|awarded|new plant|greenfield|brownfield|"
    r"inaugurated|scaling|capacity|joint venture|jv|mou|m&a)"
    r"(?:s|es|ed|ing)?\b",
    re.I,
)

# Noise regex: drop macro market news before intent matching
# Noise regex — drops only pure financial/market/legal news that has NO
# procurement signal. Removed entries:
#   • "stake", "shares?", "stock" — a "₹5,000 cr stake in new plant" IS a signal
#   • "results?" — "Company results in new facility" is real
#   • "cabinet" — "Cabinet approves ₹10,000 cr scheme" is a signal
#   • "appoints?", "resigns?", "ceo" — "CEO inaugurates new plant" is a signal
#   • "raises \$" — "Raises $500m for new plant" is a signal
# Kept: pure financial reporting (Q1 results, net profit), market indices,
# legal proceedings, and funding rounds unrelated to procurement.
NOISE = re.compile(
    r"\b(q[1-4]\b|fy\d{2}|net profit|net loss|"
    r"quarterly earnings|sensex|nifty|"
    r"price cap|"
    r"funding round|valuation|ipo|"
    r"high court|supreme court|tribunal)\b",
    re.I,
)


def _http_get(url, timeout=15, max_bytes=1_500_000):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml,application/atom+xml,"
                      "application/xml,text/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(max_bytes).decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"[news_feeds] {url[:60]}: {str(e)[:80]}")
        return ""


def _iso_date(s):
    s = str(s or "").strip()
    if not s:
        return ""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    try:
        return parsedate_to_datetime(s).date().isoformat()
    except Exception:
        return s[:30]


def _parse_feed(xml_text, max_items=20):
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        log.debug(f"[news_feeds] RSS parse failed: {str(e)[:80]}")
        return []

    is_atom = root.tag.endswith("feed")
    item_tag = "entry" if is_atom else "item"

    out = []
    for item in root.iter(item_tag):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not link and is_atom:
            for ln in item.findall("link"):
                if ln.get("rel") in (None, "alternate"):
                    link = ln.get("href") or ""
                    break
        desc = (item.findtext("description")
                or item.findtext("summary") or "").strip()
        date_raw = (item.findtext("pubDate")
                    or item.findtext("published")
                    or item.findtext("updated") or "").strip()

        if not title or not link:
            continue
        if NOISE.search(title):
            continue
        if not INTENT.search(title + " " + desc):
            continue

        out.append({
            "url": link,
            "title": title[:300],
            "snippet": re.sub(r"<[^>]+>", " ", desc)[:500],
            "date": _iso_date(date_raw),
        })
        if len(out) >= max_items:
            break
    return out


def fetch_news_feed(name, max_items=20):
    """Fetch one feed by name. Returns [] if name unknown or feed empty."""
    url = FEEDS.get(name)
    if not url:
        return []
    body = _http_get(url)
    if not body:
        return []
    return _parse_feed(body, max_items=max_items)


def fetch_all_news():
    """Fetch all configured feeds. Returns aggregated list."""
    out = []
    for name in FEEDS:
        try:
            items = fetch_news_feed(name)
            if items:
                log.info(f"[news_feeds] {name}: {len(items)} lead(s)")
            else:
                log.debug(f"[news_feeds] {name}: 0 lead(s)")
            out.extend(items)
        except Exception as e:
            log.warning(f"[news_feeds] {name}: ERROR {str(e)[:120]}")
    return out
