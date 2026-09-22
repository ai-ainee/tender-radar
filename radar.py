import os
import sys
import json
import re
import time
import urllib.parse
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
import requests

def log(msg):
    print(msg, flush=True)

log(">>> HYBRID AUTONOMOUS TENDER RADAR ACTIVE")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GOOGLE_SHEET_WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")

PRODUCTS_FILE = "products.txt"
SEEN_FILE = "seen_links.txt"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# 1. Broad Procurement Terms
PROCUREMENT_PATTERNS = re.compile(
    r"\b(tender|tenders|rfp|bid|bids|bidding|gem|eprocure|procurement|supply|quotation|eoi|nit|license|licenses|subscription|renewal|contract)\b",
    re.IGNORECASE,
)

# 2. Known Indian Procurement Authorities & Portals
ORG_PATTERNS = re.compile(
    r"\b(GeM|Government e-Marketplace|CPPP|eProcure|IIT|NIT|Railway|Railways|Metro|CPWD|DRDO|ISRO|BHEL|NTPC|ONGC|PWD|Municipal|University|AIIMS)\b",
    re.IGNORECASE,
)

# 3. Contact & Phone / Email Extractors
EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
PHONE_REGEX = re.compile(r"(?:\+91[- ]?)?[6789]\d{9}\b")

def load_products():
    if not os.path.exists(PRODUCTS_FILE):
        return ["AutoCAD", "Revit"]
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_seen(link):
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        f.write(link + "\n")

def push_to_google_sheet(product, ltype, org, contact, email, phone, summary, link):
    if not GOOGLE_SHEET_WEBHOOK:
        return
    payload = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "product": product,
        "type": ltype,
        "org": org,
        "contact_person": contact,
        "email": email,
        "phone": phone,
        "summary": summary,
        "link": link,
    }
    try:
        res = requests.post(GOOGLE_SHEET_WEBHOOK, json=payload, timeout=10)
        log(f"  -> Google Sheet updated! (HTTP {res.status_code})")
    except Exception as e:
        log(f"  -> Google Sheet Push Error: {e}")

def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        log(f"  -> Telegram dispatched! (HTTP {res.status_code})")
    except Exception as e:
        log(f"  -> Telegram Error: {e}")

def fetch_opportunities(product):
    queries = [
        f'"{product}" (site:gem.gov.in OR site:eprocure.gov.in OR site:tenderdetail.com)',
        f'"{product}" (tender OR "RFP" OR "NIT" OR "bid document") India',
    ]

    all_items = []
    seen_in_scan = set()

    for q in queries:
        encoded = urllib.parse.quote(q)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=8)
            if resp.status_code == 200 and resp.content:
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item"):
                    link = item.findtext("link", "").strip()
                    title = item.findtext("title", "").strip()
                    desc = item.findtext("description", "").strip()
                    if link and title and link not in seen_in_scan:
                        seen_in_scan.add(link)
                        all_items.append({
                            "title": title,
                            "link": link,
                            "summary": desc,
                            "product": product
                        })
        except Exception as e:
            log(f"Fetch error: {e}")

    return all_items[:12]

def extract_lead_locally(item):
    """
    Deterministic rule engine that identifies leads even if Gemini is down.
    """
    text = f"{item['title']} {item['summary']}"
    
    # Check if this item has strong procurement signals
    is_tender = bool(PROCUREMENT_PATTERNS.search(text))
    
    # Extract likely Organization
    org_match = ORG_PATTERNS.search(text)
    org = org_match.group(0) if org_match else "Govt / PSU Procurement Authority"
    
    # Identify Lead Type
    if "gem.gov.in" in text.lower() or "government e-marketplace" in text.lower():
        ltype = "GeM Bid / Procurement"
    elif "rfp" in text.lower():
        ltype = "Commercial RFP"
    elif "nit" in text.lower() or "tender" in text.lower():
        ltype = "Government Tender / NIT"
    else:
        ltype = "Software License Requirement"

    # Extract Contact Info
    emails = EMAIL_REGEX.findall(text)
    phones = PHONE_REGEX.findall(text)
    email = emails[0] if emails else "Not Listed"
    phone = phones[0] if phones else "Not Listed"

    clean_summary = re.sub(r"<[^>]+>", " ", item['summary']).strip()
    if len(clean_summary) < 20:
        clean_summary = item['title']

    return {
        "is_lead": is_tender,
        "lead_type": ltype,
        "org": org,
        "contact_person": "Procurement Officer",
        "email": email,
        "phone": phone,
        "summary": clean_summary[:160]
    }

def try_gemini_analysis(client, batch):
    """Attempts Gemini classification; gracefully returns None on 503/429."""
    if not client:
        return None

    items_block = ""
    for idx, it in enumerate(batch):
        clean_title = it['title'].replace('"', "'")
        clean_desc = re.sub(r"<[^>]+>", " ", it['summary']).replace('"', "'")[:200]
        items_block += f"\n--- ITEM {idx} ---\nTitle: {clean_title}\nSnippet: {clean_desc}\n"

    prompt = f"""
    Evaluate these Indian procurement candidate items:
    {items_block}

    Identify if each item is a tender, GeM bid, or software license procurement in India.
    Reply ONLY with a raw JSON list:
    [
      {{
        "item_index": 0,
        "is_lead": true,
        "lead_type": "GeM Bid / Govt Tender / University Lab RFP",
        "org": "Organization Name",
        "contact_person": "Officer Name or Not Listed",
        "email": "Email or Not Listed",
        "phone": "Phone or Not Listed",
        "summary": "Short 1-sentence summary"
      }}
    ]
    """

    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
        )
        raw = (
            response.text.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        return json.loads(raw)
    except Exception as e:
        log(f"  [Notice] Gemini API unavailable ({e}). Falling back to Local Rule Engine.")
        return None

def main():
    client = None
    if GEMINI_API_KEY:
        try:
            from google import genai
            client = genai.Client(api_key=GEMINI_API_KEY)
        except Exception as e:
            log(f"Client init warning: {e}")

    products = load_products()
    seen = load_seen()
    log(f"Loaded Products: {products}")

    candidates = []
    for prod in products:
        log(f"Scanning for: {prod}")
        items = fetch_opportunities(prod)
        for item in items:
            if item["link"] in seen:
                continue
            seen.add(item["link"])
            save_seen(item["link"])

            text_blob = f"{item['title']} {item['summary']}"
            if PROCUREMENT_PATTERNS.search(text_blob):
                candidates.append(item)

    log(f"Total qualified procurement items: {len(candidates)}")
    if not candidates:
        log("No new opportunities detected.")
        return

    # Try AI analysis first; fall back immediately if Gemini is down/503
    evaluations = try_gemini_analysis(client, candidates[:8])

    leads_recorded = 0
    if evaluations:
        # Use AI-parsed leads
        for res in evaluations:
            idx = res.get("item_index")
            if idx is not None and idx < len(candidates) and res.get("is_lead") is True:
                item = candidates[idx]
                leads_recorded += 1
                dispatch_lead(item, res)
    else:
        # Fallback: Process deterministically via Local Rule Engine
        log("--> Processing candidates via Local Procurement Rule Engine...")
        for item in candidates[:8]:
            res = extract_lead_locally(item)
            if res["is_lead"]:
                leads_recorded += 1
                dispatch_lead(item, res)

    log(f"\nCompleted run. Leads recorded and pushed: {leads_recorded}")

def dispatch_lead(item, data):
    prod = item["product"]
    org = data.get("org", "Govt / PSU Buyer")
    ltype = data.get("lead_type", "Procurement Notice")
    contact = data.get("contact_person", "Not Listed")
    email = data.get("email", "Not Listed")
    phone = data.get("phone", "Not Listed")
    summary = data.get("summary", item['title'])
    link = item["link"]

    log(f"\n>>> [CONFIRMED TENDER LEAD]: {item['title'][:70]}")
    log(f"    Buyer: {org} | Type: {ltype}")

    push_to_google_sheet(prod, ltype, org, contact, email, phone, summary, link)

    msg = (
        f"🚨 *New Indian Commercial Lead!*\n\n"
        f"📦 *Product:* {prod}\n"
        f"🏛 *Authority / Org:* {org}\n"
        f"📋 *Type:* {ltype}\n"
        f"👤 *Contact Person:* {contact}\n"
        f"📧 *Email:* {email}\n"
        f"📞 *Phone:* {phone}\n"
        f"📝 *Summary:* {summary}\n\n"
        f"🔗 [Open Tender Notice]({link})"
    )
    send_telegram(msg)

if __name__ == "__main__":
    main()
