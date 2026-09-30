import os
import requests
import time
import random
from dotenv import load_dotenv
import google.generativeai as genai
from ddgs import DDGS
from tenacity import retry, stop_after_attempt, wait_exponential

print(">>> 🧠 DEEP INTEL ACTIVE (Dossier Generation Engine)")

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
    available_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
    flash_models = [m for m in available_models if 'flash' in m.lower()]
    pro_models = [m for m in available_models if 'pro' in m.lower()]
    fallback_order = flash_models + pro_models + [m for m in available_models if m not in flash_models + pro_models]
    
    if not fallback_order:
        fallback_order = ['models/gemini-1.5-flash', 'models/gemini-1.5-pro'] 
        
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

# 4. Generate the Dossiers
def generate_intel_dossiers():
    print("\n[*] Scanning for missing Deep Intel Dossiers in Leads tab...")
    data = fetch_from_sheet("get_leads_intel")
    pending_intel = data.get("pending_leads", [])
    
    if not pending_intel:
        print("    -> No leads need dossiers right now.")
        return

    for lead in pending_intel:
        lead_id = lead.get('lead_id')
        org = lead.get('org', 'Unknown Company')
        industry = lead.get('industry', 'Unknown')
        dm_name = lead.get('dm_name', 'Decision Maker')
        
        print(f"    -> Generating Dossier for: {org}")
        context = web_scrape_context(f"{org} company profile business model latest news projects")
        
        prompt = f"""
        You are an elite B2B Sales Engineer. Generate a tactical 'Deep Intel Dossier' for the following account.
        
        ACCOUNT CONTEXT:
        Company: {org}
        Target Product: {industry}
        Contact: {dm_name}
        Web Context: {context}
        
        Provide a concise, highly strategic 4-part briefing. Do NOT use JSON. Use clean Markdown formatting:
        
        ### 🏢 Executive Summary
        (What this company/entity does and their market position in 2 concise sentences)
        
        ### ⚙️ Probable Tech Stack & Infrastructure
        (Based on their industry and size, what infrastructure, software, or machinery are they likely currently running?)
        
        ### 🎯 Pain Points & Buying Triggers
        (Why would they need '{industry}' right now? Identify specific regulatory, scaling, or operational friction points.)
        
        ### 🚀 Strategic Pitch Angle
        (Exactly how to open the email or call to {dm_name}. Give a 1-sentence value proposition that will hook them.)
        """
        try:
            dossier = generate_with_fallback(prompt).strip()
            send_to_sheet({
                "secret": SECRET, "action": "update_lead_dossier", "lead_id": lead_id, "dossier": dossier
            })
            print(f"       [+] Dossier successfully injected for {org}.")
        except Exception as e:
            print(f"       [-] Failed to generate dossier for {org}: {e}")

if __name__ == "__main__":
    generate_intel_dossiers()
    print("\n✅ Deep Intel Cycle Complete.")
