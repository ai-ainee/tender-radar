import os
import json
import requests
import time
from dotenv import load_dotenv
import google.generativeai as genai
from duckduckgo_search import DDGS
from tenacity import retry, stop_after_attempt, wait_exponential

print(">>> 🕵️‍♂️ DEEP HUNTER ACTIVE (Enrichment & Intel Engine)")

# 1. Load Credentials
load_dotenv()
WEBHOOK_URL = os.getenv("GOOGLE_SHEET_WEBHOOK")
SECRET = os.getenv("WEBHOOK_SECRET", "RadarEngine2026_Secure!")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not WEBHOOK_URL or not GEMINI_API_KEY:
    print("[-] ERROR: Missing .env credentials.")
    exit(1)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash')

# 2. Sheet Webhook Helpers
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

# 3. Secure Web Scraper (Upgraded with DDGS to bypass IP blocks)
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def web_scrape_context(query):
    text_data = ""
    try:
        with DDGS() as ddgs:
            results = ddgs.text(query, max_results=5)
            if results:
                for r in results:
                    text_data += f"{r.get('title', '')}: {r.get('body', '')}\n"
        time.sleep(2) # Prevent rate-limiting
    except Exception as e:
        print(f"       [-] Search engine rate limit: {e}")
        
    if not text_data:
        return "Company information restricted by search engine. Base analysis on industry standards."
    return text_data[:3000]

# 4. TASK 1: Find Stakeholders for 'Qualified' Leads
def task_1_stakeholder_enrichment():
    print("\n[*] TASK 1: Scanning for 'Pending Enrichment' in Qualified tab...")
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

# 5. TASK 2: Generate Deep Intel Dossier for 'Leads'
def task_2_deep_intel_dossier():
    print("\n[*] TASK 2: Scanning for missing Deep Intel Dossiers in Leads tab...")
    data = fetch_from_sheet("get_leads_intel")
    pending_intel = data.get("pending_leads", [])
    
    if not pending_intel:
        print("    -> No leads need dossiers right now.")
        return

    for lead in pending_intel:
        lead_id = lead['lead_id']
        org = lead['org']
        industry = lead.get('industry', 'Unknown')
        dm_name = lead.get('dm_name', 'Decision Maker')
        
        print(f"    -> Generating Dossier for: {org}")
        
        context = web_scrape_context(f"{org} company profile business model latest news projects")
        
        prompt = f"""
        You are an elite B2B Sales Engineer. Generate a tactical 'Deep Intel Dossier' for the following account.
        
        ACCOUNT CONTEXT:
        Company/Entity: {org}
        Target Product/Service: {industry}
        Primary Contact: {dm_name}
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
            response = model.generate_content(prompt)
            dossier = response.text.strip()
            
            dossier_payload = {
                "secret": SECRET,
                "action": "update_lead_dossier",
                "lead_id": lead_id,
                "dossier": dossier
            }
            send_to_sheet(dossier_payload)
            print(f"       [+] Dossier successfully injected for {org}.")
        except Exception as e:
            print(f"       [-] Failed to generate dossier for {org}: {e}")

if __name__ == "__main__":
    task_1_stakeholder_enrichment()
    task_2_deep_intel_dossier()
    print("\n✅ Deep Hunter Cycle Complete.")
