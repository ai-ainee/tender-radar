import os
import json
import requests
import time
import random
import urllib.parse
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import google.generativeai as genai
from googlesearch import search as google_search
from tenacity import retry, stop_after_attempt, wait_exponential

print(">>> 📡 RADAR SCOUT ACTIVE (V13 Geo-Bypass Engine)")

# 1. Load Credentials
load_dotenv()
WEBHOOK_URL = os.getenv("GOOGLE_SHEET_WEBHOOK")
SECRET = os.getenv("WEBHOOK_SECRET", "RadarEngine2026_Secure!")

raw_keys = os.getenv("GEMINI_API_KEY", "")
all_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]

if not WEBHOOK_URL or not all_keys:
    print("[-] ERROR: Missing .env credentials or API keys.")
    exit(1)

# 2. Webhook Helpers (Timeout increased to 30s to prevent 'Read timed out' errors)
def fetch_from_sheet(action):
    try:
        response = requests.post(WEBHOOK_URL, json={"secret": SECRET, "action": action}, timeout=30)
        return response.json()
    except Exception as e:
        print(f"[-] Webhook Error ({action}): {e}")
        return {}

def send_to_sheet(payload):
    try:
        requests.post(WEBHOOK_URL, json=payload, timeout=30)
    except Exception as e:
        print(f"[-] Failed to send payload: {e}")

print("[*] Syncing with Google Sheets CRM...")
cache_data = fetch_from_sheet("get_cache")
scraped_urls = cache_data.get("scraped_urls", [])
banned_domains = ['amazon', 'flipkart', 'ebay', 'justdial', 'youtube', 'facebook', 'twitter', 'linkedin']

# 3. Search Engine (Switched back to Google Search)
def search_web(query):
    links = []
    try:
        results = google_search(query, num_results=10, sleep_interval=3)
        for link in results:
            if link:
                if any(b in link.lower() for b in banned_domains): 
                    continue
                if link not in scraped_urls:
                    links.append(link)
        print(f"       [Found {len(links)} fresh un-scraped links]")
    except Exception as e:
        print(f"       [-] Search engine rate limit or error: {e}")
    return links

# 4. Geo-Bypass Scraper
@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=5))
def scrape_page(url):
    # This bypasses the GitHub Actions US-IP block for Indian gov/indiamart sites
    encoded_url = urllib.parse.quote(url, safe='')
    proxy_url = f"https://api.allorigins.win/get?url={encoded_url}"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    
    try:
        # Try proxy first to bypass blocks
        response = requests.get(proxy_url, timeout=20)
        response.raise_for_status()
        html = response.json().get('contents', '')
    except:
        # Fallback to direct connection
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        html = response.text
        
    soup = BeautifulSoup(html, 'html.parser')
    for script in soup(["script", "style", "nav", "footer"]):
        script.decompose()
    return soup.get_text(separator=' ', strip=True)[:4000]

# 5. AI Evaluation Engine (With API Key Rotation)
def evaluate_lead(url, text, target):
    current_key = random.choice(all_keys)
    genai.configure(api_key=current_key)
    model = genai.GenerativeModel('gemini-3.8-flash')
    
    prompt = f"""
    You are a B2B lead qualifier. Analyze this web text to see if it is a genuine commercial opportunity (Tender, RFQ, active project, or buyer requirement) related to {target}.
    
    URL: {url}
    Text: {text}
    
    Return STRICTLY in JSON format:
    {{
      "is_valid": true/false,
      "org_name": "Name of the buyer/organization (or N/A)",
      "intent": "Brief 1-sentence summary of what they need",
      "city": "City or Region (or N/A)",
      "industry": "Industry category (or N/A)"
    }}
    """
    try:
        response = model.generate_content(prompt)
        txt = response.text.strip()
        if txt.startswith("```json"): txt = txt[7:-3].strip()
        elif txt.startswith("```"): txt = txt[3:-3].strip()
        return json.loads(txt)
    except Exception as e:
        print(f"       [-] Gemini evaluation failed: {e}")
        return None

# 6. Main Radar Sequence
targets = [
    "Autodesk", "Advance Steel", "Architecture, Engineering & Construction Collection", 
    "AutoCAD", "Civil 3D", "Forma", "Autodesk Construction Cloud", "Inventor", 
    "Navisworks", "Product Design & Manufacturing Collection", "Revit", 
    "Vault Professional", "Fusion 360", "BIM"
]

def run_radar():
    for target in targets:
        queries = [
            f'"{target}" tender OR RFQ site:gov.in',
            f'"{target}" buyer requirement site:indiamart.com OR site:tradeindia.com',
            f'"{target}" upcoming project OR MOU'
        ]
        
        for q in queries:
            print(f"\n[*] Scanning: {q} (Target: {target})")
            links = search_web(q)
            
            for link in links:
                print(f"    -> Scraping: {link}")
                try:
                    text_content = scrape_page(link)
                    if not text_content: continue
                    
                    evaluation = evaluate_lead(link, text_content, target)
                    if evaluation and evaluation.get("is_valid"):
                        print(f"       [+] HOT LEAD FOUND! Org: {evaluation.get('org_name')}")
                        payload = {
                            "secret": SECRET,
                            "action": "add_lead",
                            "source_url": link,
                            "org": evaluation.get("org_name", "N/A"),
                            "industry": evaluation.get("industry", "N/A"),
                            "city": evaluation.get("city", "N/A"),
                            "intent": evaluation.get("intent", "N/A"),
                            "software_target": target
                        }
                        send_to_sheet(payload)
                    else:
                        print(f"       [-] Invalid lead, skipping.")
                        
                    scraped_urls.append(link)
                    send_to_sheet({"secret": SECRET, "action": "log_scraped_url", "url": link})
                    
                    # MANDATORY 15-SECOND COOLDOWN TO PREVENT 429 API BANS
                    print("       [Waiting 15 seconds to respect Gemini API limits...]")
                    time.sleep(15) 
                    
                except Exception as e:
                    print(f"       [-] Failed to process {link}: {e}")

if __name__ == "__main__":
    run_radar()
    print("\n✅ Radar Scout Cycle Complete.")
