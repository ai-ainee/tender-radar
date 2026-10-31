"""test_gepnic.py — pass 3: print ALL anchors + table structure."""
import os
import re

def w(s):
    print(s, flush=True)
    with open("gepnic_output.txt", "a", encoding="utf-8") as f:
        f.write(str(s) + "\n")

open("gepnic_output.txt", "w").close()

w("=" * 60)
w("GePNIC pass 3 — FULL anchor + table inspection")
w("=" * 60)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

try:
    import requests
    import urllib3
    urllib3.disable_warnings()
except ImportError:
    w("[FATAL] requests missing")
    raise SystemExit(0)

def inspect(label, url):
    w(f"\n{'='*60}\n=== {label} ===\n{'='*60}")
    w(f"URL: {url}")
    try:
        r = requests.get(url, timeout=30, verify=False,
                         headers={"User-Agent": UA, "Accept": "text/html"})
        body = r.text
    except Exception as e:
        w(f"FAILED: {e}")
        return

    w(f"HTTP {r.status_code}, {len(body)} chars")

    # ALL anchors
    anchors = re.findall(
        r'<a[^>]+href=["\']([^"\']*)["\'][^>]*>([^<]{5,300})</a>',
        body, re.I | re.S
    )
    w(f"\nAll {len(anchors)} anchors (skipping pure-nav ones):")
    NAV_WORDS = ("active tenders", "tenders by", "corrigendum", "bid awards",
                 "screen reader", "search", "home", "downloads", "debarment",
                 "announcements", "mis reports", "tenders in archive",
                 "cancelled", "results of tenders", "contact")
    for i, (href, text) in enumerate(anchors):
        t = re.sub(r"\s+", " ", text.strip())[:150]
        tl = t.lower()
        if any(nw in tl for nw in NAV_WORDS):
            continue
        h = (href or "")[:100]
        w(f"  [{i}] {t!r}")
        w(f"       href={h!r}")

    # Table row counts + first data row of the biggest table
    tables = re.findall(r"<table[^>]*>(.*?)</table>", body, re.I | re.S)
    w(f"\n{len(tables)} tables. Row counts:")
    biggest_idx, biggest_rows = -1, 0
    for i, t in enumerate(tables):
        rows = len(re.findall(r"<tr", t, re.I))
        if rows > biggest_rows:
            biggest_rows = rows
            biggest_idx = i
    for i, t in enumerate(tables):
        rows = len(re.findall(r"<tr", t, re.I))
        if rows >= 3:
            w(f"  table[{i}]: {rows} rows")
    if biggest_idx >= 0 and biggest_rows >= 3:
        w(f"\nFirst 3 <tr> of biggest table (table[{biggest_idx}]):")
        rows_html = re.findall(r"<tr[^>]*>(.*?)</tr>", tables[biggest_idx], re.I | re.S)
        for r_html in rows_html[:3]:
            cell_text = re.sub(r"<[^>]+>", " | ", r_html)
            cell_text = re.sub(r"\s+", " ", cell_text).strip()[:300]
            w(f"  {cell_text!r}")

# CPPP latest active (live tenders, no date form)
inspect("CPPP FrontEndLatestActiveTenders",
        "https://eprocure.gov.in/epublish/app?page=FrontEndLatestActiveTenders&service=page")
# Maharashtra latest active
inspect("Maharashtra FrontEndLatestActiveTenders",
        "https://mahatenders.gov.in/nicgep/app?page=FrontEndLatestActiveTenders&service=page")

w("\n" + "=" * 60)
w("DONE")
