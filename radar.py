import os
import time
import uuid
import json
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import google.generativeai as genai

# ===================================================================
# RADAR B2B CRM ENGINE - V12 (SERVICES SPLIT & POSTED DATE)
# ===================================================================

print(">>> 📡 RADAR SCOUT ACTIVE (V12 Master Engine)")

# 1. Load Credentials from .env
load_dotenv()
WEBHOOK_URL = os.getenv("GOOGLE_SHEET_WEBHOOK")
SECRET = os.getenv("WEBHOOK_SECRET", "RadarEngine2026_Secure!")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not WEBHOOK_URL or not GEMINI_API_KEY:
    print("[-] ERROR: Missing .env credentials. Please ensure GOOGLE_SHEET_WEBHOOK and GEMINI_API_KEY are set.")
    exit(1)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash')

# 2. Webhook Helper Functions
def fetch_from_sheet(action):
    try:
        response = requests.post(WEBHOOK_URL, json={"secret": SECRET, "action": action}, timeout=15)
        return response.json()
    except Exception as e:
        print(f"[-] Webhook Error ({action}): {e}")
        return {}

def send_to_sheet(payload):
    try:
        requests.post(WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"[-] Failed to send data to sheet: {e}")

# 3. Fetch Targets, Exclusions, and Cache
print("[*] Syncing with Google Sheets CRM...")
targets_data = fetch_from_sheet("get_targets")
targets = targets_data.get("targets", ["Autodesk"])

exclusions_data = fetch_from_sheet("get_exclusions")
banned_words = exclusions_data.get("exclusions", [])
banned_domains = exclusions_data.get("blocked_domains", [])

urls_data = fetch_from_sheet("get_all_urls")
scraped_urls = set(urls_data.get("urls", []))
print(f"[*] Loaded {len(scraped_urls)} existing records into cache.")

# 4. Search and Scrape Functions
def search_duckduckgo(query):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    url = f"https://html.duckduckgo.com/html/?q={query}"
    links = []
    try:
        res = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        for a in soup.find_all('a', class_='result__snippet'):
            link = a.get('href')
            if link and link.startswith('//duckduckgo.com/l/?uddg='):
                link = link.split('uddg=')[1].split('&')[0]
                import urllib.parse
                link = urllib.parse.unquote(link)
                
                # Check exclusions before even scraping
                if any(b in link.lower() for b in banned_domains): continue
                if link not in scraped_urls:
                    links.append(link)
    except Exception as e:
        print(f"[-] Search error: {e}")
    return links

def extract_text_from_url(url):
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        text = ' '.join(soup.stripped_strings)
        return text[:4000] # Limit tokens for Gemini
    except:
        return ""

# 5. Gemini AI Analysis (The Brain)
def analyze_leads_with_ai(leads_data, target_product):
    prompt = f"""
    You are an expert B2B lead generation analyst looking for: {target_product}.
    Analyze the following extracted website texts.
    
    Respond STRICTLY in this JSON format. No markdown, no extra text.
    [
      {{
        "url": "the_url_here",
        "valid_lead": true/false,
        "role": "BUYER or SELLER",
        "lead_category": "PRODUCT or SERVICE",
        "posted_date": "YYYY-MM-DD or Unknown",
        "org": "Company Name",
        "city": "City Name",
        "state": "State Name",
        "intent": "Brief summary of what they need",
        "dm_name": "Decision Maker Name if found",
        "dm_title": "Decision Maker Title",
        "reason": "If invalid, why?"
      }}
    ]

    RULES:
    1. valid_lead: False if it is spam, a job posting, or irrelevant.
    2. lead_category: MUST be 'PRODUCT' (buying software/goods) OR 'SERVICE' (maintenance, consulting, training, manpower).
    3. posted_date: Extract the original publication date.
    
    DATA TO ANALYZE:
    {json.dumps(leads_data)}
    """
    
    try:
        response = model.generate_content(prompt)
        # Clean the response to ensure valid JSON
        text = response.text.strip()
        if text.startswith("```json"): text = text[7:]
        if text.endswith("```"): text = text[:-3]
        return json.loads(text.strip())
    except Exception as e:
        print(f"[-] Gemini Analysis Failed: {e}")
        return []

# 6. The Main Execution Loop
def run():
    for target in targets:
        search_queries = [
            f'"{target}" tender OR RFQ site:gov.in',
            f'"{target}" buyer requirement site:indiamart.com OR site:tradeindia.com',
            f'"{target}" upcoming project OR MOU'
        ]

        for query in search_queries:
            print(f"\n[*] Scanning: {query} (Target: {target})")
            found_links = search_duckduckgo(query)
            
            # Batch process to save API calls
            batch_data = []
            for link in found_links[:5]: # Process top 5 new links per query
                print(f"    -> Scraping: {link}")
                text = extract_text_from_url(link)
                if text:
                    # Check banned words in text
                    if any(b.lower() in text.lower() for b in banned_words):
                        print("       [Skipped] Found banned keyword.")
                        scraped_urls.add(link)
                        continue
                    
                    batch_data.append({"url": link, "text": text})
            
            if not batch_data:
                continue
                
            print(f"    -> AI Analyzing {len(batch_data)} links...")
            ai_results = analyze_leads_with_ai(batch_data, target)
            trash_batch = []

            for entity in ai_results:
                url = entity.get("url")
                scraped_urls.add(url) # Add to cache
                
                is_valid = entity.get("valid_lead", False)
                role = entity.get("role", "BUYER").upper()
                org = entity.get("org", "Unknown")
                
                print(f"       [Vote] Valid: {is_valid} | Role: {role} | Org: {org}")

                if is_valid:
                    # 🚨 V12 STRICT ROUTING
                    lead_category = entity.get("lead_category", "PRODUCT").upper()
                    intent_label = entity.get("intent", "Unknown")
                    
                    is_supplier = (role == "SELLER")
                    is_project = "project" in intent_label.lower() or "mou" in intent_label.lower()
                    
                    if is_supplier:
                        target_sheet = "Suppliers"
                    elif is_project:
                        target_sheet = "Projects & MOUs"
                    elif lead_category == "SERVICE" or "service" in intent_label.lower():
                        target_sheet = "Services"  # Routes Service Requests here!
                    else:
                        target_sheet = "Inbox"     # Routes Product Buyers here!

                    # 🚨 V12 PERFECTED PAYLOAD
                    payload = {
                        "secret": SECRET,
                        "action": "add_lead",
                        "target_sheet": target_sheet,
                        "is_supplier": is_supplier,
                        "lead_id": str(uuid.uuid4())[:8],
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "posted_date": entity.get("posted_date", "Unknown"),
                        "source": "Radar Bot",
                        "org": org,
                        "city": entity.get("city", "Unknown"),
                        "state": entity.get("state", "Pan-India"),
                        "industry": target,
                        "intent": intent_label,
                        "dm_name": entity.get("dm_name") or "N/A",
                        "dm_title": entity.get("dm_title") or "N/A",
                        "link": url,
                        "email": "N/A",
                        "phone": "N/A",
                        "website": "N/A"
                    }
                    send_to_sheet(payload)
                else:
                    # Log to AI Trash
                    trash_batch.append({
                        "url": url, 
                        "reason": entity.get("reason", "Rejected by AI")
                    })
            
            if trash_batch:
                send_to_sheet({
                    "secret": SECRET,
                    "action": "log_trash_batch",
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "trash_data": trash_batch
                })

if __name__ == "__main__":
    run()
    print("\n✅ Radar Scout Cycle Complete.")
