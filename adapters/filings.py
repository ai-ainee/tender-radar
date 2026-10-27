"""adapters/filings.py — Corporate filings category adapter.

Structured corporate disclosures: order wins, capex, partnerships,
IPOs, regulatory actions.

Sources:
  ✅ BSE filings      — existing bse_filings.py (curl_cffi, works from GitHub)
  ✅ NSE filings      — existing nse_filings.py (curl_cffi, works from GitHub)
  ⚙ SEBI orders      — HTML scrape of sebi.gov.in (may need WAF bypass)
  ✅ RBI circulars    — RSS feed at rbi.org.in
  🔲 MCA21            — requires login, stub
  🔲 Google Patents   — requires API key or heavy scrape, stub
"""
import re
import urllib.request
import xml.etree.ElementTree as ET

from adapters import SourceAdapter, register
from standard_lead import StandardLead
from common import log

try:
    from bse_filings import fetch_bse_filings
    _HAS_BSE = True
except ImportError:
    fetch_bse_filings = None
    _HAS_BSE = False

try:
    from nse_filings import fetch_nse_filings
    _HAS_NSE = True
except ImportError:
    fetch_nse_filings = None
    _HAS_NSE = False

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _http_get(url, timeout=20, max_bytes=2_000_000):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/xml,text/xml,text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(max_bytes).decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"[filings] HTTP error {url[:60]}: {str(e)[:80]}")
        return ""


def _parse_rss(xml_text, keywords, source_name):
    if not xml_text:
        return []
    kw_lower = [str(k).lower() for k in (keywords or []) if k]
    out = []
    try:
        root = ET.fromstring(xml_text)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = (item.findtext("description") or "").strip()
            date = (item.findtext("pubDate") or "").strip()
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
        log.debug(f"[filings:{source_name}] RSS parse: {str(e)[:80]}")
    return out


def _dict_to_lead(source_name, r):
    return StandardLead(
        source="filings",
        category="filing",
        url=r.get("url", ""),
        title=str(r.get("title") or r.get("subject") or "")[:300],
        raw_text=str(r.get("snippet") or r.get("details") or "")[:3000],
        buyer_org=str(r.get("company") or "")[:200],
        raw_published_at=str(r.get("date") or ""),
        country="India",
        metadata={"internal_source": source_name},
    )


class FilingsAdapter(SourceAdapter):
    name = "filings"
    category = "filing"
    priority = 20

    def is_enabled(self, settings):
        return bool(settings.get("targets") or settings.get("industries"))

    def fetch(self, settings):
        leads = []
        for name, fetcher in [
            ("bse",     self._fetch_bse),
            ("nse",     self._fetch_nse),
            ("sebi",    self._fetch_sebi),
            ("rbi",     self._fetch_rbi),
            ("mca21",   self._fetch_mca21),
            ("patents", self._fetch_patents),
        ]:
            try:
                results = fetcher(settings)
                log.info(f"[filings] {name}: {len(results)} lead(s)")
                leads.extend(results)
            except Exception as e:
                log.warning(f"[filings] {name}: ERROR {str(e)[:120]}")
        return leads

    def _keywords(self, settings):
        """Filings never mention product names (Autocad, Revit). They mention
        capex, expansion, order wins, acquisitions, facilities. Filter by
        industry + universal business-intent signals, not by product name.
        Product relevance is judged downstream by the AI evaluator."""
        industries = settings.get("industries") or []
        industry_kw = [k for k in industries if k and k != "ALL_SECTORS"]
        # Universal business-event signals that indicate a buying event
        intent_kw = [
            "capex", "expansion", "order", "contract", "acquisition",
            "investment", "facility", "plant", "construction", "tender",
            "commissioning", "greenfield", "brownfield", "joint venture",
            "subsidiary", "IPO", "rights issue", "fundraise",
        ]
        return industry_kw + intent_kw

    def _fetch_bse(self, settings):
        if not _HAS_BSE:
            return []
        kw = self._keywords(settings)
        try:
            raw = fetch_bse_filings(kw, max_days=3, max_results=60)
        except Exception as e:
            log.debug(f"[filings:bse] {str(e)[:100]}")
            return []
        return [_dict_to_lead("bse", r) for r in (raw or []) if isinstance(r, dict) and r.get("url")]

    def _fetch_nse(self, settings):
        if not _HAS_NSE:
            return []
        kw = self._keywords(settings)
        try:
            raw = fetch_nse_filings(kw, max_results=60)
        except Exception as e:
            log.debug(f"[filings:nse] {str(e)[:100]}")
            return []
        return [_dict_to_lead("nse", r) for r in (raw or []) if isinstance(r, dict) and r.get("url")]

    def _fetch_sebi(self, settings):
        kw = self._keywords(settings)
        # SEBI publishes RSS for orders and press releases
        for url in (
            "https://www.sebi.gov.in/sebiweb/ajax/home/getnewslist.jsp",
            "https://www.sebi.gov.in/sebi_data/rss/orders.xml",
        ):
            body = _http_get(url, timeout=20)
            if not body:
                continue
            if "<item>" in body:
                items = _parse_rss(body, kw, "sebi")
                if items:
                    return [_dict_to_lead("sebi", r) for r in items]
        return []

    def _fetch_rbi(self, settings):
        kw = self._keywords(settings)
        for url in (
            "https://rbi.org.in/pressreleases_rss.xml",
            "https://www.rbi.org.in/Scripts/RSS.aspx?Id=13",
        ):
            body = _http_get(url, timeout=20)
            if body and "<item>" in body:
                items = _parse_rss(body, kw, "rbi")
                if items:
                    return [_dict_to_lead("rbi", r) for r in items]
        return []

    def _fetch_mca21(self, settings):
        log.info("[filings] mca21: not implemented (requires authentication)")
        return []

    def _fetch_patents(self, settings):
        log.info("[filings] patents: not implemented (requires API key or heavy scraping)")
        return []


register(FilingsAdapter())
