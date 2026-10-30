"""test_bse.py — ScraperAPI + BSE diagnostic. Loads keys from Sheet."""
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

# ── Step 1: Load keys the same way radar.py does ──
w("\n--- Key loading ---")
w(f"env SCRAPERAPI_API_KEYS before load: {'SET' if os.getenv('SCRAPERAPI_API_KEYS') else 'EMPTY'}")

try:
    from common import SheetAPI, inject_api_keys_from_settings
    sheet = SheetAPI()
    cfg = sheet.get("get_settings")
    api_keys_block = cfg.get("api_keys") if isinstance(cfg, dict) else None
    if api_keys_block:
        inject_api_keys_from_settings(api_keys_block)
    w(f"env SCRAPERAPI_API_KEYS after load:  {'SET' if os.getenv('SCRAPERAPI_API_KEYS') else 'EMPTY'}")
except Exception as e:
    w(f"[WARN] Could not load Settings: {e}")
    traceback.print_exc()

# ── Step 2: Check keys ──
raw = os.getenv("SCRAPERAPI_API_KEYS", "")
keys = [k.strip() for k in raw.replace('"', "").replace("'", "").split(",") if k.strip()]
w(f"SCRAPERAPI_API_KEYS count: {len(keys)}")

if not keys:
    w("[FAIL] No ScraperAPI keys found in env OR Sheet Settings")
    sys.exit(0)

# ── Step 3: Test ScraperAPI proxy ──
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
    w(f"\nURL: {url[:110]}...")

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
                        w("[NOTE] Table is empty — no announcements in 15-day window")
                else:
                    w("[FAIL] No 'Table' key in response")
            else:
                w(f"Unexpected response: {str(data)[:200]}")
        except Exception as e:
            w(f"[FAIL] JSON parse error: {e}")
    else:
        w(f"[FAIL] Non-200 response")
except Exception as e:
    w(f"[FAIL] Exception: {e}")
    traceback.print_exc()

w("=" * 60)
w("DONE")
