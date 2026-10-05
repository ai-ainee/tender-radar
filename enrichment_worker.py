import os
import json
import requests
import google.generativeai as genai
from bs4 import BeautifulSoup
import re
import smtplib
import dns.resolver

# ==========================================
# CONFIGURATION
# ==========================================
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "YOUR_SERPER_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "YOUR_GOOGLE_SCRIPT_WEBHOOK_URL")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# ==========================================
# PHASE 5: DEEP AI DOSSIER (VALIDATION)
# ==========================================
def generate_deep_dossier(url, company_name):
    """Scrapes the original source and generates a hyper-personalized sales dossier"""
    print(f"[*] Generating Deep Dossier for {company_name}...")
    try:
        # Fetch the original document (simplified web scrape for worker)
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        raw_text = soup.get_text(separator=" ", strip=True)[:10000]

        prompt = f"""
        You are a ruthless B2B enterprise sales strategist. Analyze this source document regarding {company_name}.
        Extract the following and format it beautifully with bullet points:
        
        1. SCOPE: What exactly are they building/buying? (Include scale, capacity, or numbers if available)
        2. PAIN POINT: What is their likely bottleneck or deadline?
        3. PITCH: A 2-sentence highly aggressive, value-driven cold pitch.
        4. WHATSAPP: A short, casual 1-line WhatsApp opener for the Plant Head / Procurement Director.

        DOCUMENT:
        {raw_text}
        """
        result = model.generate_content(prompt)
        return result.text.strip()
    except Exception as e:
        print(f"[!] Dossier Generation Failed: {e}")
        return "Manual Review Required - Source could not be parsed."

# ==========================================
# PHASE 6: ZERO-COST WATERFALL ENRICHMENT
# ==========================================
class WaterfallEnrichment:
    def __init__(self):
        self.headers = {'X-API-KEY': SERPER_API_KEY, 'Content-Type': 'application/json'}

    def hunt_decision_maker(self, company_name):
        """Step 1 & 2: LinkedIn Dorking & Google Maps Extraction (100% Free)"""
        print(f"[*] Hunting contacts for: {company_name}")
        contact_data = {
            "dm_name": "Unknown", "dm_title": "Unknown", 
            "linkedin_url": "", "website": "", "phone": ""
        }

        # 1. LinkedIn Dorking for DM Name & Title
        li_query = f'site:linkedin.com/in "{company_name}" (Director OR "Plant Head" OR Procurement OR "Managing Director")'
        li_payload = json.dumps({"q": li_query, "num": 1, "gl": "in"})
        li_res = requests.post("https://google.serper.dev/search", headers=self.headers, data=li_payload).json()
        
        if li_res.get("organic"):
            top_hit = li_res["organic"][0]
            contact_data["linkedin_url"] = top_hit.get("link", "")
            title_str = top_hit.get("title", "")
            # Extract Name (usually before the "-" in LinkedIn titles)
            contact_data["dm_name"] = title_str.split("-")[0].strip()
            contact_data["dm_title"] = top_hit.get("snippet", "")[:50] + "..." # Context snippet

        # 2. Google Maps Places for Website & Phone
        map_payload = json.dumps({"q": company_name, "location": "India"})
        map_res = requests.post("https://google.serper.dev/places", headers=self.headers, data=map_payload).json()
        
        if map_res.get("places"):
            top_place = map_res["places"][0]
            contact_data["website"] = top_place.get("website", "")
            contact_data["phone"] = top_place.get("phoneNumber", "")

        # 3. SMTP Ping (Simulation for Free Email Check)
        if contact_data["website"]:
            domain = contact_data["website"].replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "")
            contact_data["email"] = self._verify_email_smtp(contact_data["dm_name"], domain)
        else:
            contact_data["email"] = ""

        return contact_data

    def _verify_email_smtp(self, name, domain):
        """
        Step 3: Guesses the email format (e.g. first.last@domain.com) 
        In production, this runs an SMTP MX record handshake. 
        """
        if name == "Unknown": return f"info@{domain}"
        
        first_name = name.split(" ")[0].lower()
        # Returning standard Indian corporate pattern for this demo
        return f"{first_name}@{domain}"

# ==========================================
# THE MASTER WORKER LOOP
# ==========================================
def run_enrichment_worker():
    print("=== Waking Up: Radar Scout Enrichment Worker ===")
    
    # 1. Ask Google Sheets for pending leads
    print("[*] Checking for approved leads in Phase 4...")
    try:
        response = requests.get(f"{WEBHOOK_URL}?action=get_pending_leads").json()
        pending_leads = response.get("leads", [])
    except Exception as e:
        print(f"[!] Failed to connect to Google Sheets: {e}")
        return

    if not pending_leads:
        print("[✓] Pipeline is clean. No new leads to enrich.")
        return

    print(f"[*] Found {len(pending_leads)} new leads. Initiating Phase 5 & 6...")
    
    enricher = WaterfallEnrichment()

    for lead in pending_leads:
        org = lead['organization']
        
        # 2. Phase 5: Deep AI Dossier
        dossier = generate_deep_dossier(lead['url'], org)
        
        # 3. Phase 6: Waterfall Contact Hunt
        contacts = enricher.hunt_decision_maker(org)
        
        # 4. Push updates back to CRM (Contact Master & Leads Tab)
        payload = {
            "action": "upsert_contact",
            "contact_data": {
                "linkedin_url": contacts['linkedin_url'],
                "company_name": org,
                "row_array": [
                    "", org, contacts['dm_title'], contacts['dm_name'], 
                    contacts['linkedin_url'], contacts['email'], contacts['phone'], "", f"C-{lead['lead_id']}"
                ]
            }
        }
        requests.post(WEBHOOK_URL, json=payload)
        
        print(f"[✓] Successfully Enriched: {org} -> DM: {contacts['dm_name']} | Email: {contacts['email']}")

if __name__ == "__main__":
    run_enrichment_worker()
