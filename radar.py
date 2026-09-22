import json
import os
import urllib.parse
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
from google import genai
import requests

# 1. Load Secrets
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


def load_products():
    if not os.path.exists(PRODUCTS_FILE):
        return []
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        return [
            line.strip()
            for line in f
            if line.strip() and not line.startswith("#")
        ]


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
        res = requests.post(GOOGLE_SHEET_WEBHOOK, json=payload, timeout=15)
        print(f"  -> Sheet updated! Status: {res.status_code}")
    except Exception as e:
        print(f"  -> Sheet push error: {e}")


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
        res = requests.post(url, json=payload, timeout=12)
        print(f"  -> Telegram sent! Status: {res.status_code}")
    except Exception as e:
        print(f"  -> Telegram error: {e}")


def fetch_opportunities(product):
    """Searches live web index for tenders, GeM bids, RFPs, and corporate software licensing."""
    # Query focused on Indian procurement terminology and tender portals
    query = f'"{product}" (tender OR "RFP" OR "GeM" OR "bid" OR "procurement" OR "licenses" OR "NIT")'
    encoded = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"

    items = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code == 200 and resp.content:
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item")[:10]:
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                desc = item.findtext("description", "")
                if link and title:
                    items.append({"title": title, "link": link, "summary": desc})
    except Exception as e:
        print(f"Fetch error for {product}: {e}")

    return items


def analyze_with_ai(client, product, title, summary):
    prompt = f"""
    You are an Indian software procurement and government tender detection specialist.
    Evaluate this item for Autodesk/CAD product: "{product}".

    Title: {title}
    Snippet: {summary}

    Question: Does this mention or relate to a buying requirement, procurement process, government tender, RFP, corporate project implementation, or software licensing opportunity in India?
    (Even if it is general procurement news mentioning CAD/Autodesk software adoption or tenders, mark as true).

    Extract any contact name, email, or phone if present (otherwise return "Not Listed").

    Reply ONLY with raw JSON (no backticks or extra text):
    {{
      "is_lead": true or false,
      "lead_type": "Tender / GeM Bid / License Procurement / Project RFP",
      "org": "Name of Authority, Department, PSU, or Organization (or 'Procurement Authority')",
      "contact_person": "Officer Name or Not Listed",
      "email": "Email address or Not Listed",
      "phone": "Phone number or Not Listed",
      "summary": "1 crisp sentence summarizing the requirement"
    }}
    """
    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash", contents=prompt
        )
        clean = (
            response.text.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        return json.loads(clean)
    except Exception as e:
        print(f"  AI Parse error: {e}")
        return {"is_lead": False}


def main():
    if not GEMINI_API_KEY:
        print("Missing GEMINI_API_KEY.")
        return

    client = genai.Client(api_key=GEMINI_API_KEY)
    products = load_products()
    seen = load_seen()

    print(f"Monitoring Products: {products}")
    total_leads = 0

    for prod in products:
        print(f"\n==========================================")
        print(f"Scanning for: {prod}")
        entries = fetch_opportunities(prod)
        print(f"Found {len(entries)} items to evaluate.")

        for entry in entries:
            link = entry["link"]
            title = entry["title"]
            summary = entry["summary"]

            if link in seen:
                continue

            analysis = analyze_with_ai(client, prod, title, summary)
            is_lead = analysis.get("is_lead", False)

            if is_lead is True:
                total_leads += 1
                org = analysis.get("org", "Govt / Corporate Buyer")
                ltype = analysis.get("lead_type", "Software Procurement")
                contact = analysis.get("contact_person", "Not Listed")
                email = analysis.get("email", "Not Listed")
                phone = analysis.get("phone", "Not Listed")
                lead_summary = analysis.get("summary", "Procurement requirement identified.")

                print(f"\n[LEAD FOUND] {title[:75]}")
                print(f"  Buyer: {org} | Type: {ltype}")

                push_to_google_sheet(
                    prod, ltype, org, contact, email, phone, lead_summary, link
                )

                msg = (
                    f"🚨 *New Indian Commercial Lead!*\n\n"
                    f"📦 *Product:* {prod}\n"
                    f"🏛 *Buyer / Org:* {org}\n"
                    f"📋 *Type:* {ltype}\n"
                    f"👤 *Contact Person:* {contact}\n"
                    f"📧 *Email:* {email}\n"
                    f"📞 *Phone:* {phone}\n"
                    f"📝 *Summary:* {lead_summary}\n\n"
                    f"🔗 [Open Procurement Link]({link})"
                )
                send_telegram(msg)
            else:
                print(f"  [Filtered Out - Not a buying lead]: {title[:60]}...")

            seen.add(link)
            save_seen(link)

    print(f"\n==========================================")
    print(f"Total qualified leads logged: {total_leads}")


if __name__ == "__main__":
    main()
