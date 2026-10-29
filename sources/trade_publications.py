"""sources/trade_publications.py — construction & infrastructure RSS feeds.

These outlets publish 3-5 project announcements per day — new tenders,
contract awards, capex announcements, factory openings. High signal density
compared to general business news.

Feeds marked "best guess" may need URL adjustment if the outlet changes
their feed path. If a feed returns 0 items for 3 consecutive runs, remove it.
"""
import re
import urllib.request
import xml.etree.ElementTree as ET

from common import log

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

FEEDS = {
    "construction_world":   "https://www.constructionworld.in/rss/latest",
    "nbmcw":                "https://www.nbmcw.com/rss.xml",
    "infrastructure_today": "https://www.infrastructuretoday.co.in/rss",
    "urban_update":         "https://www.urbanupdate.in/feed",
    "metro_rail_today":     "https://metrorailtoday.com/rss.xml",
    "construction_week":    "https://www.constructionweekonline.in/rss.xml",
}

INTENT = re.compile(
    r"\b(tender|rfp|rfq|procurement|awarded|wins|contract|"
    r"expansion|facility|plant|capex|investment|commissioned|"
    r"inaugurat|greenfield|brownfield)\b",
    re.I,
)


def _http_get(url, timeout=15):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml,application/atom+xml,"
                      "application/xml,text/xml;q=0.9,*/*;q=0.8",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(1_500_000).decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"[trade_pubs] {url[:60]}: {str(e)[:80]}")
        return ""


def _parse_feed(xml_text, max_items=5):
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
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
        date = (item.findtext("pubDate")
                or item.findtext("published")
                or item.findtext("updated") or "").strip()

        if not title or not link:
            continue
        if not INTENT.search(title + " " + desc):
            continue

        out.append({
            "url": link,
            "title": title[:300],
            "snippet": re.sub(r"<[^>]+>", " ", desc)[:500],
            "date": date[:30],
        })
        if len(out) >= max_items:
            break
    return out


def fetch_trade_publications(max_items=5):
    """Aggregate items from all trade publication RSS feeds."""
    out = []
    for name, url in FEEDS.items():
        try:
            body = _http_get(url)
            if not body:
                log.debug(f"[trade_pubs] {name}: feed empty")
                continue
            items = _parse_feed(body, max_items=max_items)
            if items:
                log.info(f"[trade_pubs] {name}: {len(items)} lead(s)")
            out.extend(items)
        except Exception as e:
            log.warning(f"[trade_pubs] {name}: ERROR {str(e)[:100]}")
    return out
