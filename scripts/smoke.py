"""Smoke test for the gateway. Run from C:\\ai-gateway:  py scripts\\smoke.py"""
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
CATEGORIES = ["Food", "Groceries", "Transport", "Housing", "Shopping", "Bills", "Entertainment", "Other"]


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


print("1) health")
print("  ", call("GET", "/health"))

print("2) plain chat")
code, r = call("POST", "/v1/chat", {"messages": [{"role": "user", "content": "Say hello in five words."}]})
print("  ", code, r.get("content"), r.get("timings"))

print("3) schema-enforced categorize (with examples)")
schema = {"type": "object",
          "properties": {"category": {"type": "string", "enum": CATEGORIES}},
          "required": ["category"]}
system = ("Categorize bank transactions. Examples: 'CHIPOTLE 0423' -> Food; 'PUBLIX #88' -> Groceries; "
          "'SHELL OIL 5741' -> Transport; 'NETFLIX.COM' -> Entertainment; 'DUKE ENERGY' -> Bills. "
          "Coffee shops and restaurants are Food.")
code, r = call("POST", "/v1/chat", {
    "messages": [{"role": "system", "content": system},
                 {"role": "user", "content": "STARBUCKS #1234 TAMPA FL $6.45"}],
    "json_schema": schema, "max_tokens": 30})
print("  ", code, r.get("parsed"), "attempts:", r.get("attempts"), r.get("timings"))

print("4) stats")
print("  ", call("GET", "/stats"))
