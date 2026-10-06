import os
import json
import hashlib
import requests
import urllib3
from bs4 import BeautifulSoup
from pypdf import PdfReader
from io import BytesIO
from datetime import datetime
from google import genai
from google.genai import types
import re
import uuid

# Suppress SSL warnings for Indian Govt websites
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# MULTI-KEY AUTO-ROTATION MANAGER
# ==========================================
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
        print(f"[!] {service_name} limit hit. Rotating to Key #{self.index + 1} of {len(self.keys)}...")
        return self.get_current()

serper_keys = APIKeyManager(os.getenv("SERPER_API_KEYS", ""))
gemini_keys = APIKeyManager(os.getenv("GEMINI_API_KEYS", ""))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

CACHE_FILE = "seen_links.txt"
if not os.path.exists(CACHE_FILE):
    open(CACHE_FILE, 'w').close()

def get_cached_links():
    with open(CACHE_FILE, 'r') as f:
        return set(line.strip() for line in f)

def cache_link(link):
    with open(CACHE_FILE, 'a') as f:
        f.write(link + '\n')

# ==========================================
# 1. QUERY GENERATOR
# ==========================================
class QueryGenerator:
    def __init__(self, target_product, industry_keywords=""):
        self.target = target_product
        self.industry = industry_keywords
        self.year = datetime.now().year

    def build_tracks(self):
        ind = self.industry if self.industry != "Unknown" else ""
        return {
            "TRACK_1_TENDERS": [
                f'"{self.target}" tender OR RFP OR "procurement notice" site:eprocure.gov.in',
                f'"{self.target}" "bid document" site:gem.gov.in',
                f'"{self.target}" {ind} tender site:mahatenders.gov.in'
            ],
            "TRACK_2_CAPEX": [
                f'"{self.target}" "environmental clearance" OR "Terms of Reference" site:environmentclearance.nic.in',
                f'"{self.target}" "land allotment" OR "industrial area" (MIDC OR GIDC OR SIPCOT)',
                f'"{self.target}" "capacity expansion" OR "greenfield project" filetype:pdf'
            ],
            "TRACK_3_MCA": [
                f'"{ind}" "Incorporation Date" "{self.year}" site:zaubacorp.com'
            ],
            "TRACK_4_COMMERCIAL": [
                f'hiring "CAD Draftsman" OR "{self.target} engineer" site:naukri.com OR site:linkedin.com',
                f'"{self.target}" service provider OR consultant "India"'
            ]
        }

# ==========================================
# 2. SERPER HARVESTER (MULTI-KEY ENABLED)
# ==========================================
class SerperHarvester:
    def __init__(self, key_manager):
        self.keys = key_manager

    def _execute_search(self, endpoint, payload):
        for _ in range(len(self.keys.keys)):
            headers = {
                'X-API-KEY': self.keys.get_current(),
                'Content-Type': 'application/json'
            }
            try:
                response = requests.post(endpoint, headers=headers, data=payload, timeout=10)
                if response.status_code in [403, 429]:
                    self.keys.rotate("Serper")
                    continue
                if response.status_code == 200:
                    return response.json()
            except Exception as e:
                print(f"[!] Serper connection issue: {e}")
            return {}
        print("[!] All Serper keys exhausted.")
        return {}

    def search_web(self, query):
        print(f"[*] Executing Search: {query}")
        payload = json.dumps({"q": query, "num": 10, "gl": "in"})
        res = self._execute_search("https://google.serper.dev/search", payload)
        return res.get("organic", [])

# ==========================================
# 3. DEEP CONTENT SCRAPER (SSL FIX & TIMEOUT)
# ==========================================
class ContentScraper:
    def __init__(self):
        # Added a robust User-Agent to help bypass blocks like Indeed
        self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

    def fetch_content(self, url):
        try:
            # verify=False bypasses the Indian Govt SSL errors. timeout=10 stops the script from hanging.
            response = requests.get(url, headers=self.headers, timeout=10, verify=False)
            response.raise_for_status()

            if 'application/pdf' in response.headers.get('Content-Type', '') or url.lower().endswith('.pdf'):
                return self._parse_pdf(response.content)
            return self._parse_html(response.text)
        except Exception as e:
            print(f"[!] Scraping failed for {url}: {e}")
            return None

    def _parse_pdf(self, pdf_bytes):
        try:
            reader = PdfReader(BytesIO(pdf_bytes))
            text = "".join(page.extract_text() + "\n" for page in reader.pages[:10])
            return text.strip()
        except Exception:
            return ""

    def _parse_html(self, html_text):
        soup = BeautifulSoup(html_text, 'html.parser')
        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.decompose()
        text = soup.get_text(separator=" ", strip=True)
        return " ".join(text.split())[:15000]

# ==========================================
# 4. SPLIT-BRAIN EVALUATOR (NEW GENAI SDK)
# ==========================================
class SplitBrainEvaluator:
    def __init__(self, key_manager):
        self.keys = key_manager

    def evaluate(self, doc, target_product):
        track = doc['track']
        raw_text = doc['raw_text']
        
        if track == "TRACK_3_MCA":
            return self._evaluate_mca(raw_text)

        prompt = self._get_prompt_for_track(track, target_product)
        full_prompt = f"{prompt}\n\nDOCUMENT TEXT:\n{raw_text[:15000]}"
        
        for _ in range(len(self.keys.keys)):
            try:
                # Upgraded to the new google-genai syntax
                client = genai.Client(api_key=self.keys.get_current())
                print(f"[*] Sending to Gemini ({track})...")
                
                response = client.models.generate_content(
                    model='gemini-1.5-flash',
                    contents=full_prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                    ),
                )
                return json.loads(response.text)
                
            except Exception as e:
                error_str = str(e).lower()
                if "429" in error_str or "quota" in error_str or "exhausted" in error_str:
                    self.keys.rotate("Gemini")
                else:
                    print(f"[!] Gemini Error: {e}")
                    return {"is_valid": False, "confidence": "LOW", "reason": "AI_PARSE_ERROR"}
                    
        return {"is_valid": False, "confidence": "LOW", "reason": "ALL_GEMINI_KEYS_EXHAUSTED"}

    def _evaluate_mca(self, text):
        cin_match = re.search(r'[L|U]\d{5}[A-Z]{2}(\d{4})[A-Z]{3}\d{6}', text)
        if cin_match and cin_match.group(1) == "2026":
            return {
                "is_valid": True, "confidence": "HIGH", "entity_role": "NEW_INCORPORATION",
                "organization": "Unknown (Review Link)", "city": "Unknown", "state": "Unknown",
                "intent_brief": "New Company incorporated in 2026. Verified via CIN.", "deadline": "N/A"
            }
        return {"is_valid": False, "confidence": "LOW", "reason": "NOT_2026_OR_NO_CIN"}

    def _get_prompt_for_track(self, track, target_product):
        base_schema = """
        Respond STRICTLY in this JSON format:
        {
            "is_valid": true/false, "confidence": "HIGH" or "LOW",
            "entity_role": "(BUYER, PROJECT_BUYER, SERVICE_USER, SELLER, IRRELEVANT)",
            "organization": "Name of Company", "city": "City Name or N/A",
            "state": "State Name or N/A", "intent_brief": "2 sentence summary",
            "deadline": "YYYY-MM-DD or N/A", "reason": "If false, why?"
        }
        """
        if track == "TRACK_1_TENDERS":
            return f"You are a procurement analyst. Find {target_product} bids. If deadline is past, mark false." + base_schema
        elif track == "TRACK_2_CAPEX":
            return f"You are an industrial analyst looking for {target_product} demand in land/factory reports. Role = PROJECT_BUYER." + base_schema
        else:
            return f"You are a commercial analyst finding {target_product} usage (hiring = SERVICE_USER, distributors = SELLER)." + base_schema

# ==========================================
# 5. FAILSAFE ROUTER & CRM PUSH
# ==========================================
class WebhookRouter:
    def __init__(self, webhook_url):
        self.webhook_url = webhook_url

    def route_and_push(self, doc, ai_result, target_product):
        lead_id = str(uuid.uuid4())[:8].upper()
        capture_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        fingerprint = hashlib.md5(f"{doc['url']}".encode()).hexdigest()[:10]

        is_valid = ai_result.get('is_valid', False)
        confidence = ai_result.get('confidence', 'LOW')
        role = ai_result.get('entity_role', 'IRRELEVANT')
        
        target_sheet = "📥 Inbox"
        if not is_valid: target_sheet = "🗑️ AI_Trash"
        elif confidence == "LOW" and role in ["PROJECT_BUYER", "SERVICE_USER"]: target_sheet = "⚠️️ Needs Review"
        elif role == "SELLER": target_sheet = "🤝 Partners & Suppliers"
            
        if target_sheet == "🗑️ AI_Trash":
            row_data = [capture_date, ai_result.get('organization', 'Unknown'), ai_result.get('reason', 'Unknown'), doc['url'], doc['track'], ""]
        elif target_sheet == "🤝 Partners & Suppliers":
            row_data = [capture_date, "Dealer", ai_result.get('state', ''), ai_result.get('city', ''), ai_result.get('organization', ''), "", "", target_product]
        else:
            row_data = [
                capture_date, ai_result.get('deadline', 'N/A'), role, "Unknown", 
                ai_result.get('state', 'N/A'), ai_result.get('city', 'N/A'), ai_result.get('organization', 'Unknown'), 
                target_product, ai_result.get('intent_brief', ''), doc['url'], "", f"{lead_id}::{fingerprint}"
            ]

        try:
            requests.post(self.webhook_url, json={"action": "pre_flight_check", "company_name": ai_result.get('organization', 'Unknown')}, timeout=10)
        except: pass

        try:
            print(f"[*] Routing {ai_result.get('organization', 'Unknown')} to -> {target_sheet}")
            requests.post(self.webhook_url, json={
                "action": "insert_lead", "target_sheet": target_sheet,
                "company_name": ai_result.get('organization', 'Unknown'),
                "signal_brief": ai_result.get('intent_brief', ''), "row_data": row_data
            }, timeout=10)
        except Exception as e:
            print(f"[!] Failed to push to CRM: {e}")

# ==========================================
# 6. MASTER EXECUTION
# ==========================================
def fetch_dynamic_settings():
    print("[*] Fetching search parameters from Google Sheets...")
    try:
        response = requests.get(f"{WEBHOOK_URL}?action=get_settings", timeout=10).json()
        if response.get("status") == "success":
            return response.get("target_product"), response.get("industry_keywords")
        else:
            print("[!] Settings tab is empty. Please fill Row 2 in Google Sheets.")
            return None, None
    except Exception as e:
        print(f"[!] Failed to connect to Google Sheets for settings: {e}")
        return None, None

if __name__ == "__main__":
    print("=== Waking Up: Radar Scout Harvester ===")
    
    TARGET_PRODUCT, INDUSTRY = fetch_dynamic_settings()
    
    if not TARGET_PRODUCT or TARGET_PRODUCT == "Unknown":
        print("[!] Halting execution. No Target Product defined in Google Sheets.")
        exit()

    print(f"[*] Active Target: {TARGET_PRODUCT}")
    print(f"[*] Active Industry: {INDUSTRY}")
    
    generator = QueryGenerator(TARGET_PRODUCT, INDUSTRY)
    harvester = SerperHarvester(serper_keys)
    scraper = ContentScraper()
    evaluator = SplitBrainEvaluator(gemini_keys)
    router = WebhookRouter(WEBHOOK_URL)
    seen_links = get_cached_links()
    
    tracks = generator.build_tracks()

    for track_name, queries in tracks.items():
        print(f"\n=== Initiating {track_name} ===")
        for query in queries:
            results = harvester.search_web(query)
            for res in results:
                link = res.get("link")
                if not link or link in seen_links: continue
                
                print(f"[+] Scraping: {link}")
                content = scraper.fetch_content(link)
                if content:
                    doc = {"track": track_name, "url": link, "raw_text": content}
                    ai_verdict = evaluator.evaluate(doc, TARGET_PRODUCT)
                    router.route_and_push(doc, ai_verdict, TARGET_PRODUCT)
                    
                    cache_link(link)
                    seen_links.add(link)

    print("\n[✓] Radar Scout Cycle Complete.")
