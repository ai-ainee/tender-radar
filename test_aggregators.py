"""test_aggregators.py — pass 4: dump raw JSON for parsing fixes."""
import json
import requests

URL = "https://gateway.pipeworx.io/india-tenders/mcp"
HEADERS = {"Content-Type": "application/json"}

def call_tool_raw(name, args):
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args}
    }
    try:
        r = requests.post(URL, json=payload, headers=HEADERS, timeout=60)
        data = r.json()
        content = data.get("result", {}).get("content", [])
        if content and content[0].get("text"):
            return json.loads(content[0]["text"])
        return {"_error": "empty content", "_raw": str(data)[:500]}
    except Exception as e:
        return {"_error": str(e)}

print("=" * 60)
print("TEST A: india_cppp_latest_tenders — dump full raw response")
print("=" * 60)
# Try with no args first — many MCP tools have all-optional args
for args in [{}, {"limit": 10}, {"feed": "all"}, {"mode": "all"}]:
    print(f"\n--- args={args} ---")
    result = call_tool_raw("india_cppp_latest_tenders", args)
    if "_error" in result:
        print(f"  ERROR: {result['_error']}")
        continue
    # Print full JSON so we see EVERY field
    print(f"  Response: {json.dumps(result, indent=2)[:1500]}")

print("\n" + "=" * 60)
print("TEST B: india_defence_tenders — dump first tender's FULL schema")
print("=" * 60)
result = call_tool_raw("india_defence_tenders", {})
if "_error" in result:
    print(f"ERROR: {result['_error']}")
else:
    tenders = result.get("tenders", [])
    if tenders:
        print(f"Total tenders: {len(tenders)}")
        print(f"First tender (full JSON, all fields):")
        print(json.dumps(tenders[0], indent=2)[:1500])
        print(f"\nSample of next 4 titles:")
        for t in tenders[1:5]:
            print(f"  • {t.get('title', '')[:80]}")

print("\n" + "=" * 60)
print("TEST C: india_tender_organisations — dump first org's FULL schema")
print("=" * 60)
result = call_tool_raw("india_tender_organisations", {})
if "_error" in result:
    print(f"ERROR: {result['_error']}")
else:
    orgs = result.get("organisations", result.get("orgs", []))
    print(f"Total orgs (found key: {'organisations' if 'organisations' in result else 'orgs'}): {len(orgs)}")
    if orgs:
        print(f"First org (full JSON, all fields):")
        print(json.dumps(orgs[0], indent=2))
    print(f"\nTop 10 orgs by active tender count:")
    try:
        sorted_orgs = sorted(orgs, key=lambda x: x.get("active_tenders_listed", 0), reverse=True)
        for o in sorted_orgs[:10]:
            print(f"  • {o}")
    except Exception as e:
        print(f"  Could not sort: {e}")

print("\n" + "=" * 60)
print("DONE")
