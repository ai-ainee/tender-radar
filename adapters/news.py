"""adapters/news.py — Business news category adapter.

10 RSS sources, all free and unlimited. Merged from news + funding:
  • 7 business news feeds (ET, BS, Mint, MC, PIB, BL, FE)
  • 3 startup funding feeds (VCCircle, Inc42, Entrackr)

FILTERING PHILOSOPHY — critical:
  Business news rarely mentions product names like "Autocad" or "Revit".
  They say "L&T wins Mumbai Metro contract", "Interarch invests ₹70 cr in
  new facility", "Sun Pharma expands R&D". None of these mention the
  product — but ALL of them are buying signals.

  Therefore this adapter filters by BUSINESS INTENT keywords only
  (tender, capex, expansion, contract, order, etc.). Product-name matching
  happens downstream in the AI evaluator, which sees the full article
  and can judge relevance.

  Product-agnostic. Industry-agnostic. If you change Settings.target_products
  from Autodesk to SAP, this adapter still works — the AI evaluator does
  the product-specific filtering.

If a specific feed returns 0 for 2+ runs, disable it by commenting out its
entry in the FEEDS list below.
"""
import re
import urllib.request
import xml.etree.ElementTree as ET

from adapters import SourceAdapter, register
from standard_lead import StandardLead
from common import log

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# (source_name, feed_url) — all free, all RSS
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

# Business-intent signals — product-agnostic. An article must contain at
# least one of these to be considered a buying signal. These cover the
# universal language of B2B procurement and corporate growth.
BUSINESS_INTENT_HINTS = (
    "tender", "procurement", "expansion", "capex", "facility",
    "plant", "contract", "order", "project", "hiring", "investment",
    "acquisition", "partnership", "commission", "launch",
    "wins", "awarded", "new plant", "greenfield", "brownfield",
    "commissioned", "inaugurated", "scaling", "capacity",
    "joint venture", "JV", "MoU", "mou",
)


def _http_get(url, timeout=15, max_bytes=1_500_000):
    """Plain HTTP GET with browser UA. Returns text or '' on error."""
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


def _parse_rss(xml_text, source_name, max_items=50):
    """Parse RSS → list of dicts. Filter only by business-intent hints,
    NOT by product name. Product-name filtering happens downstream."""
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
        hay = (title + " " + desc).lower()
        # Require at least one business-intent keyword
        if not any(hint in hay for hint in BUSINESS_INTENT_HINTS):
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
    """Convert an RSS dict to StandardLead."""
    return StandardLead(
        source="news",
        category="news",
        url=r.get("url", ""),
        title=str(r.get("title", ""))[:300],
        raw_text=str(r.get("snippet", ""))[:3000],
        raw_published_at=str(r.get("date", "")),
        country="India",
        metadata={
            "fetch_method": "rss",
            "internal_source": source_name,
        },
    )


class NewsAdapter(SourceAdapter):
    name = "news"
    category = "news"
    priority = 60

    def is_enabled(self, settings):
        # Enabled when we have anything to look for — targets OR industries
        return bool(settings.get("targets") or settings.get("industries"))

    def fetch(self, settings):
        leads = []
        for source_name, url in FEEDS:
            try:
                body = _http_get(url, timeout=15)
                if not body:
                    log.info(f"[news] {source_name}: 0 lead(s) (feed empty)")
                    continue
                items = _parse_rss(body, source_name, max_items=30)
                for r in items:
                    leads.append(_dict_to_lead(source_name, r))
                log.info(f"[news] {source_name}: {len(items)} lead(s)")
            except Exception as e:
                log.warning(f"[news] {source_name}: ERROR {str(e)[:120]}")
        return leads[:200]


register(NewsAdapter())
