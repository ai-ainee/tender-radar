"""sources/moef.py — MoEF PARIVESH public hearings.

environmentclearance.nic.in publishes public-hearing notices in a static
HTML table (unlike parivesh.nic.in, which requires login). This module
scrapes the static page and extracts project names from table rows.

If the site restructures, the regex patterns will need adjustment — the
log will show `[moef] 0 hearing(s)` and the raw HTML length.

Each hearing corresponds to a ₹100+ crore industrial/infrastructure
project — the strongest forward-looking procurement signal available.
"""
import re
import urllib.request

from common import log

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

MOEF_URLS = (
    "https://environmentclearance.nic.in/dashboard_public_hearing.aspx",
    "https://environmentclearance.nic.in/",
)


def _http(url, timeout=25):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-IN,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(2_000_000).decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"[moef] http {url[:60]}: {str(e)[:80]}")
        return ""


def _extract_table_titles(html, kw_lower, max_results):
    """Extract hearing titles from any HTML table on the page."""
    out = []
    seen = set()

    # Each row → cells → first non-empty cell with length > 15 is the title
    for row in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.I | re.S):
        row_html = row.group(1)
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.I | re.S)
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                 for c in cells]
        cells = [c for c in cells if c]
        if not cells:
            continue

        # Pick the longest cell as the title (project name cell)
        title = max(cells, key=len).strip()
        if len(title) < 20 or title in seen:
            continue

        # Skip header rows
        title_low = title.lower()
        if title_low in ("project name", "project", "name of project",
                         "name", "details", "sr. no", "sr no"):
            continue

        seen.add(title)
        if kw_lower and not any(k in title_low for k in kw_lower):
            continue

        out.append({
            "url": MOEF_URLS[0],
            "title": title[:300],
            "snippet": " | ".join(cells[:3])[:500],
        })
        if len(out) >= max_results:
            break

    return out


def fetch_moef(keywords, max_results=20):
    """Return list of {url, title, snippet} from MoEF public hearings.

    Returns [] on network failure, empty page, or no matching rows.
    Never raises.
    """
    if not keywords:
        return []

    kw_lower = [str(k).lower().strip() for k in keywords if k]
    out = []

    for url in MOEF_URLS:
        html = _http(url)
        if not html or len(html) < 500:
            log.debug(f"[moef] {url[:60]}: empty or too short")
            continue
        hits = _extract_table_titles(html, kw_lower, max_results)
        if hits:
            out.extend(hits)
            break

    log.info(f"[moef] {len(out)} hearing(s)")
    return out
