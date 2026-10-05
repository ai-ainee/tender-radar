import os
import json
import requests
import google.generativeai as genai
from bs4 import BeautifulSoup

# ==========================================
# MULTI-KEY AUTO-ROTATION MANAGER
# ==========================================
class APIKeyManager:
    def __init__(self, env_string):
        # Parses comma-separated keys from the .env file
        self.keys = [k.strip() for k in env_string.split(',') if k.strip()]
        self.index = 0
        if not self.keys:
            raise ValueError("No API keys found. Check your .env file.")

    def get_current(self):
        return self.keys[self.index]

    def rotate(self, service_name):
        """Moves to the next key in the list."""
        self.index = (self.index + 1) % len(self.keys)
        print(f"[!] {service_name} Quota Hit. Rotating to Key #{self.index + 1} of {len(self.keys)}...")
        return self.get_current()

# Initialize Key Managers
serper_keys = APIKeyManager(os.getenv("SERPER_API_KEYS", "YOUR_SERPER_KEY"))
gemini_keys = APIKeyManager(os.getenv("GEMINI_API_KEYS", "YOUR_GEMINI_KEY"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "YOUR_GOOGLE_SCRIPT_WEBHOOK_URL")

# ==========================================
# PHASE 5: DEEP AI DOSSIER (WITH ROTATION)
# ==========================================
def generate_deep_dossier(url, company_name):
    print(f"[*] Generating Deep Dossier for {company_name}...")
    
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        raw_text = soup.get_text(separator=" ", strip=True)[:10000]
    except Exception as e:
        return f"Manual Review Required - Source could not be parsed: {e}"

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

    # Try all available Gemini keys before giving up
    for _ in range(len(gemini_keys.keys)):
        try:
            # Re-configure Gemini with the current active key
            genai.configure(api_key=gemini_keys.get_current())
            model = genai.GenerativeModel('gemini-1.5-flash')
            result = model.generate_content(prompt)
            return result.text.strip()
            
        except Exception as e:
            error_str = str(e).lower()
            if "429" in error_str or "quota" in error_str or "exhausted" in error_str:
                gemini_keys.rotate("Gemini")
            else:
                print(f"[!] Gemini Error: {e}")
                return "AI Parsing Error."
                
    return "Failed: All Gemini API keys exhausted."

# ==========================================
# PHASE 6: WATERFALL ENRICHMENT (WITH ROTATION)
# ==========================================
class WaterfallEnrichment:
    def _serper_post(self, endpoint, payload):
        """Wrapper to handle Serper API limits and key rotation"""
        for _ in range(len(serper_keys.keys)):
            headers = {
                'X-API-KEY': serper_keys.get_current(),
                'Content-Type': 'application/json'
            }
            response = requests.post(endpoint, headers=headers, data=payload)
            
            # 403 or 429 indicates quota/auth issues with Serper
            if response.status_code in [403, 429]:
                serper_keys.rotate("Serper")
                continue
                
            return response.json() if response.status_code == 200 else {}
            
        print("[!] All Serper keys exhausted.")
        return {}

    def hunt_decision_maker(self, company_name):
        print(f"[*] Hunting contacts for: {company_name}")
        contact_data = {
            "dm_name": "Unknown", "dm_title": "Unknown", 
            "linkedin_url": "", "website": "", "phone": "", "email": ""
        }

        # 1. LinkedIn Dorking
        li_query = f'site:linkedin.com/in "{company_name}" (Director OR "Plant Head" OR Procurement)'
        li_payload = json.dumps({"q": li_query, "num": 1, "gl": "in"})
        li_res = self._serper_post("https://google.serper.dev/search", li_payload)
        
        if li_res and li_res.get("organic"):
            top_hit = li_res["organic"][0]
            contact_data["linkedin_url"] = top_hit.get("link", "")
            title_str = top_hit.get("title", "")
            contact_data["dm_name"] = title_str.split("-")[0].strip()
            contact_data["dm_title"] = top_hit.get("snippet", "")[:50] + "..."

        # 2. Google Maps Places
        map_payload = json.dumps({"q": company_name, "location": "India"})
        map_res = self._serper_post("https://google.serper.dev/places", map_payload)
        
        if map_res and map_res.get("places"):
            top_place = map_res["places"][0]
            contact_data["website"] = top_place.get("website", "")
            contact_data["phone"] = top_place.get("phoneNumber", "")

        # 3. Email Pattern Synthesis
        if contact_data["website"]:
            domain = contact_data["website"].replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "")
            if contact_data["dm_name"] != "Unknown":
                first_name = contact_data["dm_name"].split(" ")[0].lower()
                contact_data["email"] = f"{first_name}@{domain}"
            else:
                contact_data["email"] = f"info@{domain}"

        return contact_data

# ==========================================
# THE MASTER WORKER LOOP
# ==========================================
def run_enrichment_worker():
    print("=== Waking Up: Radar Scout Enrichment Worker ===")
    
    try:
        response = requests.get(f"{WEBHOOK_URL}?action=get_pending_leads").json()
        pending_leads = response.get("leads", [])
    except Exception as e:
        print(f"[!] Failed to connect to Google Sheets: {e}")
        return

    if not pending_leads:
        print("[✓] Pipeline is clean. No new leads to enrich.")
        return

    print(f"[*] Found {len(pending_leads)} new leads. Initiating...")
    enricher = WaterfallEnrichment()

    for lead in pending_leads:
        org = lead['organization']
        dossier = generate_deep_dossier(lead['url'], org)
        contacts = enricher.hunt_decision_maker(org)
        
        # 4. Push updates back to CRM -> Leads Tab
        # Here we tell the webhook exactly which row to update the Dossier and Enriched Contacts for
        update_payload = {
            "action": "update_lead_dossier",
            "row_index": lead['row_index'],
            "dossier": dossier,
            "contacts": contacts
        }
        requests.post(WEBHOOK_URL, json=update_payload)
        
        # 5. Upsert to Contact Master
        contact_payload = {
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
        requests.post(WEBHOOK_URL, json=contact_payload)
        
        print(f"[✓] Successfully Enriched: {org}")

if __name__ == "__main__":
    run_enrichment_worker()
