import os
import sys
import json
import re
import time
import urllib.parse
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
import requests
from google import genai
from google.genai.errors import APIError

# Force immediate console flushing in GitHub Actions
def log(msg):
    print(msg, flush=True)

log(">>> RADAR ENGINE ACTIVATED")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GOOGLE_SHEET_WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")

PRODUCTS_FILE = "products.txt"
SEEN_FILE = "seen_links.txt"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

PROCUREMENT_PATTERNS = re.compile(
    r"\b(tender|tenders|rfp|bid|bids|bidding|gem|eprocure|procurement|supply|quotation|eoi|nit|license|licenses|subscription|renewal|contract)\b",
    re.IGNORECASE,
)

def load_products():
    if not os.path.exists(PRODUCTS_FILE):
        log(f"ERROR: {PRODUCTS_FILE} missing.")
        return []
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        prods = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    log(f"Products to scan: {prods}")
    return prods

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
        res = requests.post(GOOGLE_SHEET_WEBHOOK, json=payload, timeout=8)
        log(f"  -> Sheet updated! Status: {res.status_code}")
    except Exception as e:
        log(f"  -> Sheet error: {e}")

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
        res = requests.post(url, json=payload, timeout=8)
        log(f"  -> Telegram sent! Status: {res.status_code}")
    except Exception as e:
        log(f"  -> Telegram error: {e}")

def fetch_opportunities(product):
    query = f'"{product}" (site:gem.gov.in OR site:eprocure.gov.in OR tender OR RFP OR "NIT") India'
    encoded = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"
    
    items = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code == 200 and resp.content:
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item")[:10]:
                link = item.findtext("link", "").strip()
                title = item.findtext("title", "").strip()
                desc = item.findtext("description", "").strip()
                if link and title:
                    items.append({"title": title, "link": link, "summary": desc, "product": product})
    except Exception as e:
        log(f"Fetch timeout/error for {product}: {e}")
    return items

def analyze_candidates_single_call(client, items):
    items_block = ""
    for idx, it in enumerate(items):
        clean_title = it['title'].replace('"', "'")
        clean_desc = re.sub(r"<[^>]+>", " ", it['summary']).replace('"', "'")[:200]
        items_block += f"\n--- ITEM {idx} ---\nProduct: {it['product']}\nTitle: {clean_title}\nSnippet: {clean_desc}\n"

    prompt = f"""
    You are an Indian commercial tender and software procurement specialist.
    Analyze these items:
    {items_block}

    Determine if each item is an authentic Indian tender, GeM bid, university lab setup, or software licensing requirement.
    Extract officer name, official email, and phone if available (otherwise "Not Listed").

    Reply ONLY with a raw JSON array matching this exact structure:
    [
      {{
        "item_index": 0,
        "is_lead": true or false,
        "lead_type": "Govt Tender / GeM Bid / University Lab / Corporate RFP",
        "org": "Organization Name",
        "contact_person": "Officer Name or Not Listed",
        "email": "Email or Not Listed",
        "phone": "Phone or Not Listed",
        "summary": "1 sentence describing the software requirements"
      }}
    ]
    """

    for attempt in range(2):
        try:
            log(f"Contacting Gemini (Attempt {attempt + 1})...")
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
        except APIError as e:
            log(f"Gemini API Notice: {e.code}. Retrying in 10s...")
            time.sleep(10)
        except Exception as e:
            log(f"AI Error: {e}")
            break
    return []

def main():
    if not GEMINI_API_KEY:
        log("CRITICAL: GEMINI_API_KEY is not defined.")
        sys.exit(1)

    client = genai.Client(api_key=GEMINI_API_KEY)
    products = load_products()
    seen = load_seen()

    if not products:
        return

    candidates = []
    for prod in products:
        log(f"Querying web for: {prod}")
        results = fetch_opportunities(prod)
        for item in results:
            if item["link"] in seen:
                continue
            seen.add(item["link"])
            save_seen(item["link"])

            text_blob = f"{item['title']} {item['summary']}"
            if PROCUREMENT_PATTERNS.search(text_blob):
                candidates.append(item)

    log(f"Total qualified items across all queries: {len(candidates)}")
    if not candidates:
        log("No new procurement candidates found.")
        return

    # Cap at top 8 items and evaluate in ONE single API call
    batch = candidates[:8]
    evaluations = analyze_candidates_single_call(client, batch)

    leads_count = 0
    for res in evaluations:
        idx = res.get("item_index")
        if idx is not None and idx < len(batch) and res.get("is_lead") is True:
            leads_count += 1
            item = batch[idx]
            prod = item["product"]
            org = res.get("org", "Govt / Enterprise")
            ltype = res.get("lead_type", "Commercial Tender")
            contact = res.get("contact_person", "Not Listed")
            email = res.get("email", "Not Listed")
            phone = res.get("phone", "Not Listed")
            summary = res.get("summary", "Software opportunity identified.")

            log(f"\n[LEAD IDENTIFIED] {item['title']}")
            push_to_google_sheet(prod, ltype, org, contact, email, phone, summary, item["link"])

            msg = (
                f"🚨 *New Indian Commercial Lead!*\n\n"
                f"📦 *Product:* {prod}\n"
                f"🏛 *Organization:* {org}\n"
                f"📋 *Type:* {ltype}\n"
                f"👤 *Contact:* {contact}\n"
                f"📧 *Email:* {email}\n"
                f"📞 *Phone:* {phone}\n"
                f"📝 *Summary:* {summary}\n\n"
                f"🔗 [View Tender Notice]({item['link']})"
            )
            send_telegram(msg)

    log(f"\nExecution finished. Leads logged: {leads_count}")

if __name__ == "__main__":
    main()
