import os
import json
import requests
from google import genai
from google.genai import types
from bs4 import BeautifulSoup

class APIKeyManager:
    def __init__(self, env_string):
        self.keys = [k.strip() for k in env_string.split(',') if k.strip()]
        self.index = 0
        if not self.keys:
            raise ValueError("No API keys found. Check your GitHub Secrets.")

    def get_current(self):
        return self.keys[self.index]

    def rotate(self, service_name):
        self.index = (self.index + 1) % len(self.keys)
        print(f"[!] {service_name} Quota Hit. Rotating to Key #{self.index + 1} of {len(self.keys)}...")
        return self.get_current()

serper_keys = APIKeyManager(os.getenv("SERPER_API_KEYS", ""))
gemini_keys = APIKeyManager(os.getenv("GEMINI_API_KEYS", ""))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

def generate_deep_dossier(url, company_name):
    print(f"[*] Generating Deep Dossier for {company_name}...")
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers, timeout=10, verify=False)
        soup = BeautifulSoup(response.text, 'html.parser')
        raw_text = soup.get_text(separator=" ", strip=True)[:10000]
    except Exception as e:
        return f"Manual Review Required - Source could not be parsed: {e}"

    prompt = f"""
    You are a ruthless B2B enterprise sales strategist. Analyze this source document regarding {company_name}.
    Extract the following and format it beautifully with bullet points:
    1. SCOPE: What exactly are they building/buying?
    2. PAIN POINT: What is their likely bottleneck or deadline?
    3. PITCH: A 2-sentence highly aggressive, value-driven cold pitch.
    4. WHATSAPP: A short, casual 1-line WhatsApp opener for the Plant Head / Procurement Director.

    DOCUMENT:
    {raw_text}
    """

    for _ in range(len(gemini_keys.keys)):
        try:
            client = genai.Client(api_key=gemini_keys.get_current())
            result = client.models.generate_content(
                model='gemini-1.5-flash',
                contents=prompt
            )
            return result.text.strip()
        except Exception as e:
            error_str = str(e).lower()
            if "429" in error_str or "quota" in error_str or "exhausted" in error_str:
                gemini_keys.rotate("Gemini")
            else:
                return "AI Parsing Error."
    return "Failed: All Gemini API keys exhausted."

class WaterfallEnrichment:
    def _serper_post(self, endpoint, payload):
        for _ in range(len(serper_keys.keys)):
            headers = {'X-API-KEY': serper_keys.get_current(), 'Content-Type': 'application/json'}
            try:
                response = requests.post(endpoint, headers=headers, data=payload, timeout=10)
                if response.status_code in [403, 429]:
                    serper_keys.rotate("Serper")
                    continue
                return response.json() if response.status_code == 200 else {}
            except:
                return {}
        return {}

    def hunt_decision_maker(self, company_name):
        print(f"[*] Hunting contacts for: {company_name}")
        contact_data = {
            "dm_name": "Unknown", "dm_title": "Unknown", 
            "linkedin_url": "", "website": "", "phone": "", "email": ""
        }

        li_query = f'site:linkedin.com/in "{company_name}" (Director OR "Plant Head" OR Procurement)'
        li_res = self._serper_post("https://google.serper.dev/search", json.dumps({"q": li_query, "num": 1, "gl": "in"}))
        
        if li_res and li_res.get("organic"):
            top_hit = li_res["organic"][0]
            contact_data["linkedin_url"] = top_hit.get("link", "")
            contact_data["dm_name"] = top_hit.get("title", "").split("-")[0].strip()
            contact_data["dm_title"] = top_hit.get("snippet", "")[:50] + "..."

        map_res = self._serper_post("https://google.serper.dev/places", json.dumps({"q": company_name, "location": "India"}))
        if map_res and map_res.get("places"):
            top_place = map_res["places"][0]
            contact_data["website"] = top_place.get("website", "")
            contact_data["phone"] = top_place.get("phoneNumber", "")

        if contact_data["website"]:
            domain = contact_data["website"].replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "")
            if contact_data["dm_name"] != "Unknown":
                contact_data["email"] = f"{contact_data['dm_name'].split(' ')[0].lower()}@{domain}"
            else:
                contact_data["email"] = f"info@{domain}"

        return contact_data

def run_enrichment_worker():
    print("=== Waking Up: Radar Scout Enrichment Worker ===")
    try:
        response = requests.get(f"{WEBHOOK_URL}?action=get_pending_leads", timeout=10).json()
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
        
        requests.post(WEBHOOK_URL, json={
            "action": "update_lead_dossier", "row_index": lead['row_index'],
            "dossier": dossier, "contacts": contacts
        }, timeout=10)
        
        requests.post(WEBHOOK_URL, json={
            "action": "upsert_contact",
            "contact_data": {
                "linkedin_url": contacts['linkedin_url'], "company_name": org,
                "row_array": ["", org, contacts['dm_title'], contacts['dm_name'], contacts['linkedin_url'], contacts['email'], contacts['phone'], "", f"C-{lead['lead_id']}"]
            }
        }, timeout=10)
        
        print(f"[✓] Successfully Enriched: {org}")

if __name__ == "__main__":
    run_enrichment_worker()
