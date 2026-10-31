"""test_aggregators.py — pass 3: test the RIGHT tools."""
import json
import requests

URL = "https://gateway.pipeworx.io/india-tenders/mcp"
HEADERS = {"Content-Type": "application/json"}

def call_tool(name, args, timeout=60):
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args}
    }
    try:
        r = requests.post(URL, json=payload, headers=HEADERS, timeout=timeout)
        data = r.json()
        content = data.get("result", {}).get("content", [])
        if content and content[0].get("text"):
            return json.loads(content[0]["text"])
        return {"_error": "empty response", "_raw": str(data)[:300]}
    except Exception as e:
        return {"_error": str(e)}

print("=" * 60)
print("TEST 1: india_cppp_latest_tenders (the real aggregator)")
print("=" * 60)

for mode in ["cppp", "central"]:
    print(f"\n--- mode={mode} ---")
    result = call_tool("india_cppp_latest_tenders", {"mode": mode})
    if "_error" in result:
        print(f"ERROR: {result['_error']}")
        continue
    tenders = result.get("tenders", [])
    print(f"Returned: {len(tenders)} tenders")
    print(f"Total active (from meta): {result.get('portal_active_tenders', '?')}")
    for i, t in enumerate(tenders[:5]):
        title = (t.get("title") or "")[:100]
        closing = t.get("closing_date") or ""
        print(f"  {i+1}. {title!r}")
        if closing:
            print(f"     closing={closing}")
        # Check for description body
        desc = (t.get("description") or t.get("tender_description") or "")[:150]
        if desc:
            print(f"     desc={desc!r}")

print("\n" + "=" * 60)
print("TEST 2: india_defence_tenders (defence buyer)")
print("=" * 60)

result = call_tool("india_defence_tenders", {})
if "_error" in result:
    print(f"ERROR: {result['_error']}")
else:
    tenders = result.get("tenders", [])
    print(f"Returned: {len(tenders)} tenders")
    for i, t in enumerate(tenders[:3]):
        title = (t.get("title") or "")[:100]
        print(f"  {i+1}. {title!r}")

print("\n" + "=" * 60)
print("TEST 3: india_tender_organisations (target CAD-friendly buyers)")
print("=" * 60)

result = call_tool("india_tender_organisations", {})
if "_error" in result:
    print(f"ERROR: {result['_error']}")
else:
    # Look for departments likely to buy CAD
    orgs = result.get("organisations", [])
    print(f"Total orgs: {len(orgs)}")
    CAD_FRIENDLY = ("railway", "rail", "metro", "defence", "pwd", "cpwd",
                    "nhai", "bsnl", "works", "engineer", "construction",
                    "infrastructure", "ministry of road", "highway")
    hits = [o for o in orgs if any(k in (o.get("organisation", "")).lower() for k in CAD_FRIENDLY)]
    print(f"CAD-friendly orgs found: {len(hits)}")
    for o in hits[:10]:
        print(f"  • {o.get('organisation')}: {o.get('active_tenders_listed')} tenders")

print("\n" + "=" * 60)
print("DONE")
