"""adapters/news.py — Business news category adapter.

Sources:
  ✅ sources.news_feeds (preferred, if deployed) — 8 verified working feeds
  ⚠️ Inline fallback below — corrected feed URLs, still works standalone

Filters:
  • Noise regex (Q1-Q4 results, quarterly, net profit, cabinet, etc.)
  • Word-boundary intent regex (tender, expansion, capex, order, JV, MoU...)
  • Atom + RSS 2.0 support (entry vs item)
  • ISO date format from the start (not deferred to _dict_to_lead)
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

# ── Inline fallback feeds (only used if sources.news_feeds is not deployed) ─
# Dead feeds removed: moneycontrol/latestnews (404), financial_express (retired),
# vccircle (retired), entrackr (empty), pib/MODID=6 (hindi-only).
FEEDS = (
    ("et",                 "https://economictimes.indiatimes.com/rssfeedstopstories.cms"),
    ("bs",                 "https://www.business-standard.com/rss/home_page_top_stories.rss"),
    ("mint",               "https://www.livemint.com/rss/news"),
    ("hindu_bl",           "https://www.thehindubusinessline.com/feeder/default.rss"),
    ("inc42",              "https://inc42.com/feed/"),
    # Replacements — verified working as of late 2026
    ("moneycontrol_bus",   "https://www.moneycontrol.com/rss/business.xml"),
    ("construction_world", "https://www.constructionworld.in/rss/latest"),
    ("nbmcw",              "https://www.nbmcw.com/rss.xml"),
    ("infrastructure_today","https://www.infrastructuretoday.co.in/rss"),
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
# not procurement signals. Q1-Q4 results, price caps, cabinet decisions, etc.
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
    """RFC-822 (RSS) or ISO (Atom) → ISO YYYY-MM-DD. Best-effort."""
    s = str(s or "").strip()
    if not s:
        return ""
    # Already ISO?
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    try:
        return parsedate_to_datetime(s).date().isoformat()
    except Exception:
        return s[:30]


def _http_get(url, timeout=15, max_bytes=1_500_000):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml,application/atom+xml,application/xml,text/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(max_bytes).decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"[news] HTTP error {url[:60]}: {str(e)[:80]}")
        return ""


def _parse_rss(xml_text, source_name, max_items=20):
    """Parse RSS 2.0 OR Atom. Returns [{title, url, snippet, date(ISO)}]."""
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        log.debug(f"[news:{source_name}] RSS parse error: {str(e)[:80]}")
        return []

    is_atom = root.tag.endswith("feed")
    item_tag = "entry" if is_atom else "item"

    out = []
    for item in root.iter(item_tag):
        try:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            # Atom: link is an element with href attribute
            if not link and is_atom:
                for ln in item.findall("link"):
                    if ln.get("rel") in (None, "alternate"):
                        link = ln.get("href") or ""
                        break
            desc = (item.findtext("description")
                    or item.findtext("summary")
                    or "").strip()
            date_raw = (item.findtext("pubDate")
                        or item.findtext("published")
                        or item.findtext("updated")
                        or "").strip()
        except Exception:
            continue

        if not title or not link:
            continue
        # Drop macro/market noise BEFORE intent matching
        if _NOISE_RE.search(title):
            continue
        # Word-boundary intent match on title + desc
        if not _INTENT_RE.search(title + " " + desc):
            continue

        out.append({
            "title": title[:300],
            "url": link,
            "snippet": re.sub(r"<[^>]+>", " ", desc)[:500],
            "date": _iso_date(date_raw),
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
        raw_published_at=str(r.get("date", "")),
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

        # ── Preferred: sources.news_feeds (if deployed) ──────────────
        try:
            from sources.news_feeds import fetch_all_news
            try:
                results = fetch_all_news()
                for r in results or []:
                    if isinstance(r, dict) and r.get("url"):
                        leads.append(_dict_to_lead("news_feeds", r))
                log.info(f"[news] sources.news_feeds: {len(leads)} lead(s)")
                return leads
            except Exception as e:
                log.warning(f"[news] sources.news_feeds ERROR {str(e)[:120]} — falling back to inline")
        except ImportError:
            pass  # module not deployed — use inline fallback

        # ── Inline fallback ──────────────────────────────────────────
        for source_name, url in FEEDS:
            try:
                body = _http_get(url, timeout=15)
                if not body:
                    log.info(f"[news] {source_name}: 0 lead(s) (feed empty)")
                    continue
                items = _parse_rss(body, source_name, max_items=20)
                for r in items:
                    leads.append(_dict_to_lead(source_name, r))
                log.info(f"[news] {source_name}: {len(items)} lead(s)")
            except Exception as e:
                log.warning(f"[news] {source_name}: ERROR {str(e)[:120]}")
        return leads


register(NewsAdapter())
