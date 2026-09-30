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
raw_keys = os.getenv("GEMINI_API_KEY", "")
all_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]

if not WEBHOOK_URL or not all_keys:
    print("[-] ERROR: Missing .env credentials or API keys.")
    exit(1)

GEMINI_API_KEY = random.choice(all_keys)
genai.configure(api_key=GEMINI_API_KEY)

# --- DYNAMIC MODEL FALLBACK ENGINE ---
def generate_with_fallback(prompt):
    # Ask Google API what models this key is allowed to use
    available_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
    
    # Sort them to try 'flash' (fastest) first, then 'pro'
    flash_models = [m for m in available_models if 'flash' in m.lower()]
    pro_models = [m for m in available_models if 'pro' in m.lower()]
    fallback_order = flash_models + pro_models + [m for m in available_models if m not in flash_models + pro_models]
    
    if not fallback_order:
        fallback_order = ['models/gemini-1.5-flash', 'models/gemini-1.5-pro'] # Safe baseline
        
    for model_name in fallback_order:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            print(f"       [!] {model_name} failed/rate-limited. Switching to next model...")
            time.sleep(2)
            
    raise Exception("All available Gemini models failed or hit rate limits.")
# -------------------------------------

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
        print(f"       [-] Search engine limit: {e}")
    return text_data[:3000] if text_data else "Info restricted. Base analysis on standards."

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
        Extract decision makers, website, and phone for '{org}'. Context: {context}
        Return STRICTLY in JSON:
        {{"website": "...", "phone": "...", "primary_dm": {{"name": "...", "title": "...", "email": "..."}}, "other_contacts": []}}
        """
        try:
            txt = generate_with_fallback(prompt).strip()
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
        except Exception as e:
            print(f"       [-] Failed AI extraction for {org}: {e}")

if __name__ == "__main__":
    task_1_stakeholder_enrichment()
    print("\n✅ Deep Hunter Cycle Complete.")
