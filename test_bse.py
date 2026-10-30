# --- ScraperAPI Proxy Test ---
print("\n--- ScraperAPI Proxy Test ---")
try:
    import requests
    import urllib3
    urllib3.disable_warnings()
    from datetime import datetime, timedelta
    import os

    api_key = os.getenv("SCRAPERAPI_API_KEYS", "").split(",")[0].strip()
    if not api_key:
        print("[SKIP] SCRAPERAPI_API_KEYS not set")
    else:
        # Use the same URL as the production code
        to_date = datetime.now().strftime("%Y%m%d")
        from_date = (datetime.now() - timedelta(days=15)).strftime("%Y%m%d")
        api_url = (
            f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
            f"?pageno=1&strCat=-1&strPrevDate={from_date}&strScrip="
            f"&strSearch=P&strToDate={to_date}&strType=C&subcategory="
        )
        proxies = {
            "http": f"http://scraperapi:{api_key}@proxy-server.scraperapi.com:8001",
            "https": f"http://scraperapi:{api_key}@proxy-server.scraperapi.com:8001",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0",
            "Accept": "application/json",
            "Referer": "https://www.bseindia.com/corporates/ann.html",
        }
        r = requests.get(api_url, proxies=proxies, headers=headers, timeout=60, verify=False)
        print(f"  Status: {r.status_code}")
        print(f"  Body preview: {r.text[:300]!r}")
        if r.status_code == 200:
            try:
                data = r.json()
                if isinstance(data, dict) and "Table" in data:
                    print(f"  SUCCESS: Found {len(data['Table'])} rows in 'Table'")
                else:
                    print(f"  Response keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
            except Exception as e:
                print(f"  JSON parse failed: {e}")
except Exception as e:
    print(f"  ScraperAPI test failed: {e}")
