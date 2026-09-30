import os
import json
import requests
import time
import random
from dotenv import load_dotenv
import google.generativeai as genai
from duckduckgo_search import DDGS
from tenacity import retry, stop_after_attempt, wait_exponential

print(">>> 🕵️‍♂️ DEEP HUNTER ACTIVE (Stakeholder Enrichment Engine)")

# 1. Load Credentials & Handle Multiple API Keys
load_dotenv()
WEBHOOK_URL = os.getenv("GOOGLE_SHEET_WEBHOOK")
SECRET = os.getenv("WEBHOOK_SECRET", "RadarEngine2026_Secure!")

# API Key Rotation Logic
raw_keys = os.getenv("GEMINI_API_KEY", "")
all_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]

if not WEBHOOK_URL or not all_keys:
    print("[-] ERROR: Missing .env credentials or API keys.")
    exit(1)

# Pick a random key for this run
GEMINI_API_KEY = random.choice(all_keys)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash')

# 2. Webhook Helpers
def fetch_from_sheet(action):
    try:
        response = requests.post(WEBHOOK_URL, json={"secret": SECRET, "action": action}, timeout=15)
        return response.json()
    except Exception as e:
        print(f"[-] Webhook Error ({action}): {e}")
        return {}

def send_to_sheet(payload):
    try:
        requests.post(WEBHOOK_URL, json=payload, timeout=15)
    except Exception as e:
        print(f"[-] Failed to send payload: {e}")

# 3. Secure Web Scraper
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def web_scrape_context(query):
    text_data = ""
    try:
        with DDGS() as ddgs:
            results = ddgs.text(query, max_results=5)
            if results:
                for r in results:
                    text_data += f"{r.get('title', '')}: {r.get('body', '')}\n"
        time.sleep(2)
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
        
        prompt = f"""
        Extract decision makers (CEO, CTO, Directors, Procurement, etc.), the official company website, and a main phone number for the entity '{org}'.
        Use this scraped web data: {context}
        
        Return STRICTLY in JSON format:
        {{
          "website": "https://...",
          "phone": "...",
          "primary_dm": {{"name": "...", "title": "...", "email": "..."}},
          "other_contacts": [
            {{"name": "...", "designation": "...", "email": "...", "phone": "...", "source": "..."}}
          ]
        }}
        If a field is missing or unknown, output "N/A".
        """
        try:
            response = model.generate_content(prompt)
            txt = response.text.strip()
            if txt.startswith("```json"): txt = txt[7:-3].strip()
            elif txt.startswith("```"): txt = txt[3:-3].strip()
            result = json.loads(txt)
            
            dm = result.get("primary_dm", {})
            update_payload = {
                "secret": SECRET,
                "action": "update_lead",
                "lead_id": lead_id,
                "website": result.get("website", "N/A"),
                "phone": result.get("phone", "N/A"),
                "dm_name": dm.get("name", "N/A"),
                "dm_title": dm.get("title", "N/A"),
                "email": dm.get("email", "N/A")
            }
            send_to_sheet(update_payload)
            
            others = result.get("other_contacts", [])
            if others:
                contact_payload = {
                    "secret": SECRET,
                    "action": "add_contacts",
                    "lead_id": lead_id,
                    "org": org,
                    "contacts": others
                }
                send_to_sheet(contact_payload)
            print(f"       [+] Enriched {org}. Found {len(others)} extra contacts.")
        except Exception as e:
            print(f"       [-] Failed to parse Gemini response for {org}: {e}")

if __name__ == "__main__":
    task_1_stakeholder_enrichment()
    print("\n✅ Deep Hunter Cycle Complete.")
