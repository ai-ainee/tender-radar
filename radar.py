import os
import json
import hashlib
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from io import BytesIO
from datetime import datetime

# ==========================================
# CONFIGURATION & API KEYS
# ==========================================
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "YOUR_SERPER_KEY")
CACHE_FILE = "seen_links.txt"

# Ensure cache file exists
if not os.path.exists(CACHE_FILE):
    open(CACHE_FILE, 'w').close()

def get_cached_links():
    with open(CACHE_FILE, 'r') as f:
        return set(line.strip() for line in f)

def cache_link(link):
    with open(CACHE_FILE, 'a') as f:
        f.write(link + '\n')

# ==========================================
# 1. QUERY GENERATOR (THE 8 TIERS -> 4 TRACKS)
# ==========================================
class QueryGenerator:
    def __init__(self, target_product, industry_keywords=""):
        self.target = target_product
        self.industry = industry_keywords
        self.year = datetime.now().year

    def build_tracks(self):
        """Maps your 8 Intelligence Tiers into 4 isolated Serper Tracks"""
        return {
            "TRACK_1_TENDERS": [
                f'"{self.target}" tender OR RFP OR "procurement notice" site:eprocure.gov.in',
                f'"{self.target}" "bid document" site:gem.gov.in',
                f'"{self.target}" {self.industry} tender site:mahatenders.gov.in'
            ],
            "TRACK_2_CAPEX": [
                f'"{self.target}" "environmental clearance" OR "Terms of Reference" site:environmentclearance.nic.in',
                f'"{self.target}" "land allotment" OR "industrial area" (MIDC OR GIDC OR SIPCOT)',
                f'"{self.target}" "capacity expansion" OR "greenfield project" filetype:pdf'
            ],
            "TRACK_3_MCA": [
                # Finds Zauba/TCC profiles for newly incorporated companies in your sector
                f'"{self.industry}" "Incorporation Date" "{self.year}" site:zaubacorp.com'
            ],
            "TRACK_4_COMMERCIAL": [
                f'hiring "CAD Draftsman" OR "{self.target} engineer" site:naukri.com OR site:linkedin.com',
                # This will be routed to Google Maps Places API later, but web fallback here:
                f'"{self.target}" service provider OR consultant "India"'
            ]
        }

# ==========================================
# 2. SERPER HARVESTER (SEARCH & MAPS)
# ==========================================
class SerperHarvester:
    def __init__(self, api_key):
        self.headers = {
            'X-API-KEY': api_key,
            'Content-Type': 'application/json'
        }

    def search_web(self, query):
        """Executes Google Web Search via Serper"""
        print(f"[*] Executing Search: {query}")
        payload = json.dumps({"q": query, "num": 10, "gl": "in"})
        response = requests.post("https://google.serper.dev/search", headers=self.headers, data=payload)
        
        if response.status_code == 200:
            return response.json().get("organic", [])
        return []

    def search_places(self, query, location="India"):
        """Executes Google Maps Places Search for Track 4 MSMEs"""
        print(f"[*] Executing Maps Search: {query} in {location}")
        payload = json.dumps({"q": query, "location": location})
        response = requests.post("https://google.serper.dev/places", headers=self.headers, data=payload)
        
        if response.status_code == 200:
            return response.json().get("places", [])
        return []

# ==========================================
# 3. DEEP CONTENT SCRAPER (HTML & PDF)
# ==========================================
class ContentScraper:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
        }

    def fetch_content(self, url):
        """Detects if link is PDF or HTML and extracts text safely."""
        try:
            response = requests.get(url, headers=self.headers, timeout=15)
            response.raise_for_status()

            # Handle PDF Documents (Crucial for Tenders & Capex Reports)
            if 'application/pdf' in response.headers.get('Content-Type', '') or url.lower().endswith('.pdf'):
                return self._parse_pdf(response.content)
            
            # Handle Standard HTML
            return self._parse_html(response.text)

        except Exception as e:
            print(f"[!] Scraping failed for {url}: {e}")
            return None

    def _parse_pdf(self, pdf_bytes):
        """Extracts text from PDF buffers"""
        try:
            reader = PdfReader(BytesIO(pdf_bytes))
            text = ""
            for page in reader.pages[:10]: # Limit to first 10 pages to save AI tokens
                text += page.extract_text() + "\n"
            return text.strip()
        except Exception as e:
            print(f"[!] PDF Parse Error: {e}")
            return ""

    def _parse_html(self, html_text):
        """Strips scripts/styles and extracts readable text"""
        soup = BeautifulSoup(html_text, 'html.parser')
        
        # Remove noisy elements
        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.decompose()
            
        text = soup.get_text(separator=" ", strip=True)
        # Compress whitespace
        return " ".join(text.split())[:15000] # Cap at 15k characters for Gemini context

# ==========================================
# 4. MAIN HARVESTER EXECUTION FLOW
# ==========================================
def run_harvester(target_product, industry_keywords):
    generator = QueryGenerator(target_product, industry_keywords)
    harvester = SerperHarvester(SERPER_API_KEY)
    scraper = ContentScraper()
    seen_links = get_cached_links()
    
    tracks = generator.build_tracks()
    harvested_data = []

    for track_name, queries in tracks.items():
        print(f"\n=== Initiating {track_name} ===")
        
        for query in queries:
            results = harvester.search_web(query)
            
            for res in results:
                link = res.get("link")
                if not link or link in seen_links:
                    continue
                
                print(f"[+] Found new lead source: {link}")
                content = scraper.fetch_content(link)
                
                if content:
                    harvested_data.append({
                        "track": track_name,
                        "url": link,
                        "title": res.get("title", ""),
                        "snippet": res.get("snippet", ""),
                        "raw_text": content
                    })
                    cache_link(link)
                    seen_links.add(link)

    return harvested_data

if __name__ == "__main__":
    # Test the Harvester
    print("Starting Radar Scout Phase 1...")
    # Example input - this will eventually be pulled from your ⚙️ Settings tab
    results = run_harvester(target_product="AutoCAD", industry_keywords="Architecture OR Fabrication")
    print(f"\n[✓] Harvest Complete. Extracted {len(results)} new raw documents ready for AI Evaluation.")

import google.generativeai as genai
import re
import uuid
import json

# ==========================================
# CONFIGURATION
# ==========================================
# Get your API key from Google AI Studio
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)

# The Web App URL you got from Google Apps Script in Milestone 1
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "YOUR_GOOGLE_SCRIPT_WEBHOOK_URL")

# ==========================================
# PHASE 2: SPLIT-BRAIN AI EVALUATOR
# ==========================================
class SplitBrainEvaluator:
    def __init__(self):
        # We use Gemini 1.5 Flash - it is incredibly fast and cheap for large text parsing
        self.model = genai.GenerativeModel(
            'gemini-1.5-flash',
            generation_config={"response_mime_type": "application/json"}
        )

    def evaluate(self, doc, target_product):
        """Routes the document to the correct AI track based on Phase 1"""
        track = doc['track']
        raw_text = doc['raw_text']
        
        # TRACK 3 (MCA) bypasses AI for strict Python regex
        if track == "TRACK_3_MCA":
            return self._evaluate_mca(raw_text)

        # Build the track-specific prompt
        prompt = self._get_prompt_for_track(track, target_product)
        full_prompt = f"{prompt}\n\nDOCUMENT TEXT:\n{raw_text[:15000]}"
        
        try:
            print(f"[*] Sending to Gemini ({track})...")
            response = self.model.generate_content(full_prompt)
            result = json.loads(response.text)
            return result
        except Exception as e:
            print(f"[!] AI Evaluation Failed: {e}")
            return {"is_valid": False, "confidence": "LOW", "reason": "AI_PARSE_ERROR"}

    def _evaluate_mca(self, text):
        """Strict Regex for Indian Corporate Identity Numbers (CIN)"""
        # Matches formats like U74999DL2026PTC123456
        cin_match = re.search(r'[L|U]\d{5}[A-Z]{2}(\d{4})[A-Z]{3}\d{6}', text)
        if cin_match:
            year = cin_match.group(1)
            if year == "2026": # Ensures it's a current-year registration
                return {
                    "is_valid": True,
                    "confidence": "HIGH",
                    "entity_role": "NEW_INCORPORATION",
                    "organization": "Unknown (Review Link)", # Requires manual glance
                    "city": "Unknown",
                    "state": "Unknown",
                    "intent_brief": f"New Company incorporated in 2026. Verified via CIN.",
                    "deadline": "N/A"
                }
        return {"is_valid": False, "confidence": "LOW", "reason": "NOT_2026_OR_NO_CIN"}

    def _get_prompt_for_track(self, track, target_product):
        """Returns JSON-enforced prompts based on the specific intelligence track"""
        
        base_schema = """
        Respond STRICTLY in this JSON format:
        {
            "is_valid": true/false,
            "confidence": "HIGH" or "LOW",
            "entity_role": "(BUYER, PROJECT_BUYER, SERVICE_USER, SELLER, IRRELEVANT)",
            "organization": "Name of Company or Dept",
            "city": "City Name or N/A",
            "state": "State Name or N/A",
            "intent_brief": "2 sentence summary of what they need/are doing",
            "deadline": "YYYY-MM-DD or N/A",
            "reason": "If is_valid is false, why? (e.g., EXPIRED_TENDER, CONSUMER_SPAM)"
        }
        """

        if track == "TRACK_1_TENDERS":
            return f"You are a B2B procurement analyst looking for {target_product} bids. Look for active deadlines. If the submission deadline has already passed (earlier than today in 2026), mark is_valid as false. Extract the procuring department name." + base_schema
            
        elif track == "TRACK_2_CAPEX":
            return f"You are an industrial intelligence analyst looking for {target_product} demand. You are evaluating land allotments, factory expansions, or EPC project awards. IGNORE past publication dates (a 6-month-old land allotment means they are building NOW). Mark entity_role as PROJECT_BUYER." + base_schema
            
        elif track == "TRACK_4_COMMERCIAL":
            return f"You are a commercial intent analyst looking for {target_product} usage. If a company is hiring CAD Draftsmen/Engineers, they are a SERVICE_USER (Valid). If they are an OEM/Distributor selling the product, mark as SELLER. Do not reject small local MSMEs." + base_schema


# ==========================================
# PHASE 3: FAILSAFE ROUTER & CRM PUSH
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
        
        # 1. Routing Logic Matrix (Maps to the 9-Tab Sheet Layout)
        target_sheet = "📥 Inbox"
        
        if not is_valid:
            target_sheet = "🗑️ AI_Trash"
        elif confidence == "LOW" and role in ["PROJECT_BUYER", "SERVICE_USER"]:
            target_sheet = "⚠️ Needs Review" # The Safety Net
        elif role == "SELLER":
            target_sheet = "🤝 Partners & Suppliers"
            
        # 2. Build the Row Array (Must match Google Sheet Columns exactly)
        if target_sheet == "🗑️ AI_Trash":
            # ["Timestamp", "Organization", "AI Reject Reason", "Original URL", "Source Track", "Action / Rescue To"]
            row_data = [capture_date, ai_result.get('organization', 'Unknown'), ai_result.get('reason', 'Unknown'), doc['url'], doc['track'], ""]
        elif target_sheet == "🤝 Partners & Suppliers":
            # ["Date Added", "Partner Type", "State", "City", "Company Name", "Website", "Contact Details", "Products Sold"]
            row_data = [capture_date, "Dealer", ai_result.get('state', ''), ai_result.get('city', ''), ai_result.get('organization', ''), "", "", target_product]
        else:
            # Active Tabs (Inbox / Needs Review)
            # ["Capture Date", "Deadline / Post Date", "Signal Category", "Sector / Industry", "State", "City", "Organization", "Target Product", "AI Intent Brief", "Source Link", "Action / Move To", "Lead ID & Fingerprint"]
            row_data = [
                capture_date,
                ai_result.get('deadline', 'N/A'),
                role,
                "Unknown", # Sector (Can be updated manually or added to AI prompt)
                ai_result.get('state', 'N/A'),
                ai_result.get('city', 'N/A'),
                ai_result.get('organization', 'Unknown'),
                target_product,
                ai_result.get('intent_brief', ''),
                doc['url'],
                "", # Leave Action empty for manual UI selection
                f"{lead_id}::{fingerprint}"
            ]

        # 3. Pre-Flight Check (Global Deduplication)
        try:
            print(f"[*] Pre-Flight Check for: {ai_result.get('organization', 'Unknown')}")
            check_payload = {
                "action": "pre_flight_check",
                "company_name": ai_result.get('organization', 'Unknown')
            }
            res = requests.post(self.webhook_url, json=check_payload).json()
            if res.get('exists') and target_sheet == "📥 Inbox":
                print(f"[!] Company already in {res.get('location')}. Webhook will append note automatically.")
                # We still send it, the Google Script logic will convert it to a note!
        except Exception as e:
            print(f"[!] Pre-flight check failed: {e}")

        # 4. Transmit Payload to Google Sheets
        payload = {
            "action": "insert_lead",
            "target_sheet": target_sheet,
            "company_name": ai_result.get('organization', 'Unknown'),
            "signal_brief": ai_result.get('intent_brief', ''),
            "row_data": row_data
        }

        try:
            print(f"[*] Routing {ai_result.get('organization', 'Unknown')} to -> {target_sheet}")
            requests.post(self.webhook_url, json=payload)
        except Exception as e:
            print(f"[!] Failed to push to CRM: {e}")

# ==========================================
# 5. MASTER EXECUTION (PHASE 1 -> 2 -> 3)
# ==========================================
if __name__ == "__main__":
    TARGET_PRODUCT = "AutoCAD"
    INDUSTRY = "Architecture OR Fabrication"
    
    # 1. Run Harvester (From Milestone 2)
    # harvested_docs = run_harvester(TARGET_PRODUCT, INDUSTRY)
    
    # FOR TESTING: Let's mock a harvested document
    harvested_docs = [{
        "track": "TRACK_4_COMMERCIAL",
        "url": "https://www.naukri.com/sample-job",
        "title": "Hiring AutoCAD Draftsman - Pune",
        "raw_text": "We are a leading fabrication firm in Pune. We urgently require 2 AutoCAD draftsmen for detailing heavy machinery components. Apply immediately."
    }]

    evaluator = SplitBrainEvaluator()
    router = WebhookRouter(WEBHOOK_URL)

    for doc in harvested_docs:
        # 2. Split-Brain Evaluation
        ai_verdict = evaluator.evaluate(doc, TARGET_PRODUCT)
        print(f"\n[AI Verdict]: {json.dumps(ai_verdict, indent=2)}")
        
        # 3. Safely Route & Push to CRM
        router.route_and_push(doc, ai_verdict, TARGET_PRODUCT)
        
    print("\n[✓] Radar Scout V17 Engine Cycle Complete.")
