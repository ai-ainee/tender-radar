"""test_bse.py — ScraperAPI + BSE diagnostic. Always writes output."""
import os
import sys
import traceback

def w(s):
    print(s, flush=True)
    with open("bse_diagnose_output.txt", "a", encoding="utf-8") as f:
        f.write(s + "\n")

open("bse_diagnose_output.txt", "w").close()

w("=" * 60)
w("SCRAPERAPI BSE TEST")
w("=" * 60)

# Check keys
raw = os.getenv("SCRAPERAPI_API_KEYS", "")
keys = [k.strip() for k in raw.replace('"', "").replace("'", "").split(",") if k.strip()]
w(f"SCRAPERAPI_API_KEYS count: {len(keys)}")

if not keys:
    w("[FAIL] No ScraperAPI keys set in env")
    sys.exit(0)

try:
    import requests
    import urllib3
    urllib3.disable_warnings()
    from datetime import datetime, timedelta

    key = keys[0]
    to_date = datetime.now().strftime("%Y%m%d")
    from_date = (datetime.now() - timedelta(days=15)).strftime("%Y%m%d")

    url = (
        "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
        f"?pageno=1&strCat=-1&strPrevDate={from_date}&strScrip="
        f"&strSearch=P&strToDate={to_date}&strType=C&subcategory="
    )
    w(f"URL: {url[:110]}...")

    proxies = {
        "http":  f"http://scraperapi:{key}@proxy-server.scraperapi.com:8001",
        "https": f"http://scraperapi:{key}@proxy-server.scraperapi.com:8001",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.bseindia.com/corporates/ann.html",
        "Origin": "https://www.bseindia.com",
    }

    w("Requesting via ScraperAPI proxy (timeout 90s)...")
    r = requests.get(url, proxies=proxies, headers=headers, timeout=90, verify=False)

    w(f"Status: {r.status_code}")
    w(f"Body[:500]: {r.text[:500]!r}")

    if r.status_code == 200:
        try:
            data = r.json()
            w(f"Parsed type: {type(data).__name__}")
            if isinstance(data, dict):
                w(f"Keys: {list(data.keys())}")
                if "Table" in data and isinstance(data["Table"], list):
                    n = len(data["Table"])
                    w(f"SUCCESS: {n} rows in Table")
                    if n > 0:
                        w(f"First row keys: {list(data['Table'][0].keys())[:10]}")
                        w(f"First row sample: {str(data['Table'][0])[:300]}")
                    else:
                        w("[NOTE] Table is present but empty — no announcements in window")
                else:
                    w("[FAIL] No 'Table' key in response")
            elif isinstance(data, list):
                w(f"SUCCESS (list): {len(data)} items")
            else:
                w(f"Unexpected response type")
        except Exception as e:
            w(f"[FAIL] JSON parse error: {e}")
    else:
        w(f"[FAIL] Non-200 response")

except Exception as e:
    w(f"[FAIL] Exception: {e}")
    traceback.print_exc()

w("=" * 60)
w("DONE")
