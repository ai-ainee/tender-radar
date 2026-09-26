import os
import csv
import time
import re
import requests
import urllib.parse
from bs4 import BeautifulSoup

try:
    from duckduckgo_search import DDGS
except ImportError:
    DDGS = None

try:
    from googlesearch import search as google_organic_search
except ImportError:
    google_organic_search = None

CSV_URL = os.environ.get("QUALIFIED_CSV_URL")
WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
})

def get_search_results(query, max_res=3):
    results = []
    # Attempt 1: DuckDuckGo Lite
    if DDGS:
        try:
            ddgs = DDGS()
            res = list(ddgs.text(query, max_results=max_res, backend="lite"))
            for r in res:
                results.append({"link": r.get("href", ""), "title": r.get("title", "")})
            return results
        except Exception:
            pass
            
    # Attempt 2: Google Organic Fallback
    if google_organic_search:
        try:
            time.sleep(3)
            urls = list(google_organic_search(query, num=max_res, stop=max_res, pause=3))
            for u in urls:
                results.append({"link": u, "title": u})
            return results
        except Exception:
            pass
    return results

def hunt():
    if not CSV_URL or not WEBHOOK:
        print("❌ Missing QUALIFIED_CSV_URL or GOOGLE_SHEET_WEBHOOK environment variables.")
        return

    print(">>> 🕵️‍♂️ INITIATING DEEP HUNTER ENRICHMENT SEQUENCE...")
    try:
        r = SESSION.get(CSV_URL, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"❌ Error fetching Live CSV: {e}")
        return
        
    lines = r.text.splitlines()
    reader = csv.DictReader(lines)
    
    for row in reader:
        lead_id = row.get("Lead ID", "").strip()
        if not lead_id: continue
        
        org = row.get("Organization", "").strip()
        email = row.get("Email", "").strip()
        phone = row.get("Phone", "").strip()
        dm_name = row.get("Decision Maker", "").strip()
        website = row.get("Website", "").strip()
        
        # Check if this lead actually needs enrichment
        needs_enrich = False
        if email in ["N/A", "Not Listed", ""] or "⚠️" in email: needs_enrich = True
        if phone in ["N/A", "Not Listed", ""]: needs_enrich = True
        if dm_name in ["N/A", "Not Listed", ""]: needs_enrich = True
        
        if not needs_enrich:
            continue
            
        print(f"\n[*] Target Locked: {org} (Searching for missing data...)")
        
        # 1. FIND OFFICIAL DOMAIN
        new_web = website
        if new_web in ["N/A", "Not Listed", ""]:
            res = get_search_results(f'"{org}" official website india', 3)
            for item in res:
                link = item['link']
                if not any(x in link for x in ["linkedin.com", "zaubacorp", "justdial", "facebook", "indiamart"]):
                    parsed = urllib.parse.urlparse(link)
                    new_web = f"{parsed.scheme}://{parsed.netloc}"
                    print(f"    -> Found Domain: {new_web}")
                    break
                    
        # 2. FIND LINKEDIN DECISION MAKER
        new_dm = dm_name
        new_title = row.get("DM Title", "Not Listed")
        new_li = row.get("DM LinkedIn", "N/A")
        
        if new_dm in ["N/A", "Not Listed", ""]:
            res = get_search_results(f'"{org}" (Director OR Procurement OR "Purchase Manager" OR "Head of BIM" OR "Design Head") site:linkedin.com/in/', 2)
            if res:
                new_li = res[0]['link']
                raw_title = res[0]['title'].replace(" | LinkedIn", "").replace(" - LinkedIn", "")
                parts = raw_title.split(" - ")
                new_dm = parts[0]
                if len(parts) > 1:
                    new_title = parts[1]
                print(f"    -> Found Decision Maker: {new_dm} ({new_title})")
                    
        # 3. DIRECT WEBSITE SCRAPE FOR EXACT PHONE/EMAIL
        new_email = email
        new_phone = phone
        if new_web and new_web not in ["N/A", "Not Listed", ""]:
            try:
                time.sleep(3)
                contact_url = new_web + "/contact"
                web_r = SESSION.get(contact_url, timeout=10)
                if web_r.status_code != 200:
                    web_r = SESSION.get(new_web, timeout=10) # Try homepage if /contact fails
                    
                if web_r.status_code == 200:
                    text = web_r.text
                    # Extract Emails and Indian Phones
                    found_emails = re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", text)
                    found_phones = re.findall(r"(?:\+91[- ]?)?[6789]\d{9}\b", text)
                    
                    valid_emails = [e for e in found_emails if not e.endswith(('.png', '.jpg', '.jpeg', '.gif', '.css', '.js'))]
                    
                    if valid_emails and (new_email in ["N/A", "Not Listed", ""] or "⚠️" in new_email):
                        new_email = valid_emails[0]
                        print(f"    -> Found Exact Email: {new_email}")
                    if found_phones and new_phone in ["N/A", "Not Listed", ""]:
                        new_phone = found_phones[0]
                        print(f"    -> Found Exact Phone: {new_phone}")
            except Exception as e:
                print(f"    -> Website Scrape Failed: {e}")
                
        # 4. IF NEW DATA WAS FOUND, PUSH TO GOOGLE SHEETS BACKDOOR
        if new_dm == dm_name and new_email == email and new_phone == phone:
            print(f"    -> No fresh data found for {org}. Moving on.")
            continue
            
        payload = {
            "action": "deep_enrich",
            "lead_id": lead_id,
            "new_dm_name": new_dm,
            "new_dm_title": new_title,
            "new_linkedin": new_li,
            "new_email": new_email,
            "new_phone": new_phone,
            "new_website": new_web
        }
        
        try:
            SESSION.post(WEBHOOK, json=payload, timeout=10)
            print(f"    ✅ Successfully updated CRM for {org}!")
        except Exception as e:
            print(f"    ❌ Failed to push update to CRM: {e}")
            
        time.sleep(5) # Delay to respect Webhook rate limits

if __name__ == "__main__":
    hunt()
