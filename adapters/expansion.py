"""adapters/expansion.py — Expansion & capex category adapter.

Sources:
  ✅ sources.moef              (preferred, if deployed) — MoEF PARIVESH hearings
  ✅ sources.trade_publications (preferred, if deployed) — 6 construction RSS feeds
  ⚙ Inline fallback — MoEF HTML scrape + Invest India + 10times (all best-effort)

All inline fetchers are known-unreliable in datacenter IPs. The preferred
path is the new sources.* modules which use more robust extraction.
The inline code below is a fallback that avoids total adapter failure.
"""
import re
import urllib.request
import xml.etree.ElementTree as ET

from adapters import SourceAdapter, register
from standard_lead import StandardLead
from common import log, fetch_page

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _http_get(url, timeout=20, max_bytes=2_000_000):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/xml,application/atom+xml,text/xml,text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(max_bytes).decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"[expansion] HTTP error {url[:60]}: {str(e)[:80]}")
        return ""


def _parse_rss(xml_text, keywords, source_name):
    """Parse RSS 2.0 or Atom. Returns [{title,url,snippet,date}]."""
    if not xml_text:
        return []
    kw_lower = [str(k).lower() for k in (keywords or []) if k]
    out = []
    try:
        root = ET.fromstring(xml_text)
        is_atom = root.tag.endswith("feed")
        item_tag = "entry" if is_atom else "item"
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
            if kw_lower:
                hay = (title + " " + desc).lower()
                if not any(k in hay for k in kw_lower):
                    continue
            out.append({
                "title": title[:300],
                "url": link,
                "snippet": re.sub(r"<[^>]+>", " ", desc)[:500],
                "date": date[:30],
            })
    except Exception as e:
        log.debug(f"[expansion:{source_name}] RSS parse: {str(e)[:80]}")
    return out


def _dict_to_lead(source_name, r):
    return StandardLead(
        source="expansion",
        category="expansion",
        url=r.get("url", ""),
        title=str(r.get("title", ""))[:300],
        raw_text=str(r.get("snippet", ""))[:3000],
        raw_published_at=str(r.get("date", "")),
        country="India",
        metadata={"internal_source": source_name},
    )


class ExpansionAdapter(SourceAdapter):
    name = "expansion"
    category = "expansion"
    priority = 45

    def is_enabled(self, settings):
        return bool(settings.get("targets") or settings.get("industries"))

    def fetch(self, settings):
        leads = []
        for name, fetcher in [
            ("moef",          self._fetch_moef),
            ("trade_pubs",    self._fetch_trade_publications),
            ("invest_india",  self._fetch_invest_india),
            ("10times",       self._fetch_10times),
            ("pli",           self._fetch_pli),
            ("eventbrite",    self._fetch_eventbrite),
        ]:
            try:
                results = fetcher(settings)
                log.info(f"[expansion] {name}: {len(results)} lead(s)")
                leads.extend(results)
            except Exception as e:
                log.warning(f"[expansion] {name}: ERROR {str(e)[:120]}")
        return leads

    def _keywords(self, settings):
        return (settings.get("targets") or []) + (settings.get("industries") or [])

    def _fetch_moef(self, settings):
        """MoEF PARIVESH — public hearing listings.

        Preferred: sources.moef (table-aware extraction).
        Fallback: inline regex (best-effort, often empty).
        """
        kw = self._keywords(settings)

        # Preferred path
        try:
            from sources.moef import fetch_moef
            raw = fetch_moef(kw, max_results=20)
            if raw:
                return [_dict_to_lead("moef", r) for r in raw
                        if isinstance(r, dict) and r.get("url")]
        except ImportError:
            pass
        except Exception as e:
            log.warning(f"[expansion] moef (sources) ERROR {str(e)[:120]}")

        # Inline fallback
        for url in (
            "https://environmentclearance.nic.in/dashboard_public_hearing.aspx",
            "https://parivesh.nic.in/",
        ):
            try:
                content = fetch_page(url, max_chars=20000)
            except Exception:
                continue
            if not content or len(content) < 300:
                continue
            out = []
            kw_low = [k.lower() for k in kw]
            for m in re.finditer(
                r"(?:Public\s+Hearing|Notice)[^\n]{0,300}(?:for|at)\s+([A-Z][A-Za-z0-9&.,\s()\-]{10,200})",
                content, re.I,
            ):
                title = m.group(0).strip()[:300]
                if kw_low and not any(k in title.lower() for k in kw_low):
                    continue
                out.append({"title": title, "url": url, "snippet": title, "date": ""})
                if len(out) >= 20:
                    break
            if out:
                return [_dict_to_lead("moef", r) for r in out]
        return []

    def _fetch_trade_publications(self, settings):
        """Construction / infrastructure trade publications via sources.trade_publications."""
        try:
            from sources.trade_publications import fetch_trade_publications
        except ImportError:
            return []
        try:
            raw = fetch_trade_publications(max_items=5)
            return [_dict_to_lead("trade_pubs", r) for r in raw
                    if isinstance(r, dict) and r.get("url")]
        except Exception as e:
            log.warning(f"[expansion] trade_pubs ERROR {str(e)[:120]}")
            return []

    def _fetch_invest_india(self, settings):
        """Invest India — FDI announcements RSS (best-effort; often 0)."""
        kw = self._keywords(settings)
        for url in (
            "https://www.investindia.gov.in/rss.xml",
            "https://www.investindia.gov.in/feed",
        ):
            body = _http_get(url, timeout=20)
            if body and ("<item>" in body or "<entry>" in body):
                items = _parse_rss(body, kw, "invest_india")
                if items:
                    return [_dict_to_lead("invest_india", r) for r in items]
        return []

    def _fetch_10times(self, settings):
        """10times India — trade show listings (JS-rendered, best-effort; often 0)."""
        kw = self._keywords(settings)
        try:
            content = fetch_page("https://10times.com/india", max_chars=30000)
        except Exception:
            return []
        if not content or len(content) < 300:
            return []
        out = []
        kw_low = [k.lower() for k in kw]
        for m in re.finditer(
            r"([A-Z][A-Za-z0-9&.\-\s]{10,120})\s+(?:Trade\s+Show|Conference|Expo|Exhibition)",
            content, re.I,
        ):
            title = m.group(0).strip()[:300]
            if kw_low and not any(k in title.lower() for k in kw_low):
                continue
            out.append({
                "title": title,
                "url": "https://10times.com/india",
                "snippet": title,
                "date": "",
            })
            if len(out) >= 20:
                break
        return [_dict_to_lead("10times", r) for r in out]

    def _fetch_pli(self, settings):
        log.info("[expansion] pli: not implemented (PDF lists, needs manual parse)")
        return []

    def _fetch_eventbrite(self, settings):
        log.info("[expansion] eventbrite: not implemented (requires API key)")
        return []


register(ExpansionAdapter())
