"""adapters/news.py — Business news category adapter.

10 RSS sources, all free. Filter by business-intent signals (tender, capex,
expansion, contract) — NOT by product name. Product matching happens
downstream in the AI evaluator.

All fixes applied:
  • Word-boundary regex (no more "order" matching "border")
  • Case-insensitive intent matching (JV, MoU, M&A all work)
  • No arbitrary lead cap (keeps all feeds, cuts per-feed items instead)
  • ISO date format (RFC-822 → YYYY-MM-DD)
"""
import re
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from adapters import SourceAdapter, register
from standard_lead import StandardLead
from common import log

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

FEEDS = (
    ("et",                "https://economictimes.indiatimes.com/rssfeedstopstories.cms"),
    ("bs",                "https://www.business-standard.com/rss/home_page_top_stories.rss"),
    ("mint",              "https://www.livemint.com/rss/news"),
    ("moneycontrol",      "https://www.moneycontrol.com/rss/latestnews.xml"),
    ("pib",               "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3"),
    ("hindu_bl",          "https://www.thehindubusinessline.com/feeder/default.rss"),
    ("financial_express", "https://www.financialexpress.com/feed/"),
    ("vccircle",          "https://www.vccircle.com/feed/"),
    ("inc42",             "https://inc42.com/feed/"),
    ("entrackr",          "https://entrackr.com/feed/"),
)

BUSINESS_INTENT_HINTS = (
    "tender", "procurement", "expansion", "capex", "facility",
    "plant", "contract", "order", "project", "hiring", "investment",
    "acquisition", "partnership", "commission", "launch",
    "wins", "awarded", "new plant", "greenfield", "brownfield",
    "commissioned", "inaugurated", "scaling", "capacity",
    "joint venture", "jv", "mou", "m&a",
)

# Word-boundary regex — prevents "order" matching "border", allows plurals
_INTENT_RE = re.compile(
    r"\b(?:" + "|".join(
        sorted({re.escape(h.lower()) for h in BUSINESS_INTENT_HINTS}, key=len, reverse=True)
    ) + r")(?:s|es|ed|ing)?\b",
    re.I,
)

# Noise titles that match business-intent hints but are macro/market news,
# not procurement signals. From run 3: 25+ rejections from Hindu BusinessLine
# with quarterly results, board changes, cabinet approvals.
_NOISE_RE = re.compile(
    r"\b("
    r"q[1-4]\b|fy\d{2}|net profit|net loss|results?\b|quarterly|"
    r"stake|shares?|stock|sensex|nifty|earnings|dividend|"
    r"cabinet|price cap|appoints?|resigns?|ceo|"
    r"raises? \$|funding round|valuation|ipo|"
    r"high court|supreme court|tribunal"
    r")\b",
    re.I,
)


def _iso_date(s):
    """RFC-822 (RSS) → ISO YYYY-MM-DD. Falls back to original on error."""
    try:
        return parsedate_to_datetime(s).date().isoformat()
    except Exception:
        return s


def _http_get(url, timeout=15, max_bytes=1_500_000):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml,application/xml,text/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(max_bytes).decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"[news] HTTP error {url[:60]}: {str(e)[:80]}")
        return ""


def _parse_rss(xml_text, source_name, max_items=20):
    if not xml_text:
        return []
    out = []
    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        log.debug(f"[news:{source_name}] RSS parse error: {str(e)[:80]}")
        return []

    for item in root.iter("item"):
        try:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = (item.findtext("description") or "").strip()
            date = (item.findtext("pubDate") or "").strip()
        except Exception:
            continue
        if not title or not link:
            continue
        # Drop macro/market noise BEFORE intent matching
        if _NOISE_RE.search(title):
            continue
        # Word-boundary intent matching (case-insensitive)
        if not _INTENT_RE.search(title + " " + desc):
            continue
        out.append({
            "title": title[:300],
            "url": link,
            "snippet": re.sub(r"<[^>]+>", " ", desc)[:500],
            "date": date[:30],
        })
        if len(out) >= max_items:
            break
    return out


def _dict_to_lead(source_name, r):
    return StandardLead(
        source="news",
        category="news",
        url=r.get("url", ""),
        title=str(r.get("title", ""))[:300],
        raw_text=str(r.get("snippet", ""))[:3000],
        raw_published_at=_iso_date(str(r.get("date", ""))),   # ISO format
        country="India",
        metadata={"fetch_method": "rss", "internal_source": source_name},
    )


class NewsAdapter(SourceAdapter):
    name = "news"
    category = "news"
    priority = 60

    def is_enabled(self, settings):
        return bool(settings.get("targets") or settings.get("industries"))

    def fetch(self, settings):
        leads = []
        for source_name, url in FEEDS:
            try:
                body = _http_get(url, timeout=15)
                if not body:
                    log.info(f"[news] {source_name}: 0 lead(s) (feed empty)")
                    continue
                items = _parse_rss(body, source_name, max_items=20)   # per-feed cap, not global
                for r in items:
                    leads.append(_dict_to_lead(source_name, r))
                log.info(f"[news] {source_name}: {len(items)} lead(s)")
            except Exception as e:
                log.warning(f"[news] {source_name}: ERROR {str(e)[:120]}")
        return leads   # no global cap — all 10 feeds contribute


register(NewsAdapter())
