import os
import json
import requests
import time
import random
from dotenv import load_dotenv
import google.generativeai as genai
from googlesearch import search as google_search
from tenacity import retry, stop_after_attempt, wait_exponential

print(">>> 🕵️‍♂️ DEEP HUNTER ACTIVE (Stakeholder Enrichment Engine)")

# 1. Load Credentials & Handle Multiple API Keys
load_dotenv()
WEBHOOK_URL = os.getenv("GOOGLE_SHEET_WEBHOOK")
SECRET = os.getenv("WEBHOOK_SECRET", "RadarEngine2026_Secure!")

raw_keys = os.getenv("GEMINI_API_KEY", "")
all_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]

if not WEBHOOK_URL or not all_keys:
    print("[-] ERROR: Missing .env credentials or API keys.")
    exit(1)

# 2. Webhook Helpers
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

# 3. Secure Web Scraper (Switched from DuckDuckGo to Google Search)
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def web_scrape_context(query):
    text_data = ""
    try:
        # advanced=True gets us the text snippets directly from Google's results
        results = google_search(query, num_results=5, sleep_interval=2, advanced=True)
        for r in results:
            text_data += f"{r.title}: {r.description}\n"
    except Exception as e:
        print(f"       [-] Search engine rate limit: {e}")
        
    if not text_data:
        return "Company information restricted. Base analysis on industry standards."
    return text_data[:3000]

# 4. TASK 1: Find Stakeholders
def task_1_stakeholder_enrichment():
    print("\n[*] Scanning for 'Pending Enrichment' in Qualified tab...")
    data = fetch_from_sheet("get_pending")
    pending_leads = data.get("pending_leads", [])
    
    if not pending_leads:
        print("    -> No leads pending enrichment.")
        return

    for lead in pending_leads:
        lead_id = lead['lead_id']
        org = lead['org']
        print(f"    -> Hunting stakeholders for: {org}")
        
        context = web_scrape_context(f"{org} {lead.get('city', '')} CEO CTO Director procurement contact")
        
        # Swap API keys per request to avoid Google's Rate Limits
        current_key = random.choice(all_keys)
        genai.configure(api_key=current_key)
        model = genai.GenerativeModel('gemini-3.8-flash')
        
        prompt = f"""
        Extract decision makers, website, and phone for '{org}'. Context: {context}
        Return STRICTLY in JSON:
        {{"website": "...", "phone": "...", "primary_dm": {{"name": "...", "title": "...", "email": "..."}}, "other_contacts": []}}
        """
        try:
            response = model.generate_content(prompt)
            txt = response.text.strip()
            if txt.startswith("```json"): txt = txt[7:-3].strip()
            elif txt.startswith("```"): txt = txt[3:-3].strip()
            result = json.loads(txt)
            
            dm = result.get("primary_dm", {})
            send_to_sheet({
                "secret": SECRET, "action": "update_lead", "lead_id": lead_id,
                "website": result.get("website", "N/A"), "phone": result.get("phone", "N/A"),
                "dm_name": dm.get("name", "N/A"), "dm_title": dm.get("title", "N/A"), "email": dm.get("email", "N/A")
            })
            
            others = result.get("other_contacts", [])
            if others:
                send_to_sheet({"secret": SECRET, "action": "add_contacts", "lead_id": lead_id, "org": org, "contacts": others})
            print(f"       [+] Enriched {org}. Found {len(others)} extra contacts.")
            
            # MANDATORY 15-SECOND COOLDOWN
            print("       [Waiting 15 seconds to respect Gemini API limits...]")
            time.sleep(15)
            
        except Exception as e:
            print(f"       [-] Failed AI extraction for {org}: {e}")

if __name__ == "__main__":
    task_1_stakeholder_enrichment()
    print("\n✅ Deep Hunter Cycle Complete.")
