"""Week 1 benchmark: measure local model speed and basic accuracy through Ollama's API.

Usage (from C:\\ai-gateway\\benchmarks):
    py bench.py                      # benchmarks llama3.2:3b
    py bench.py llama3.2:3b llama3.2:1b

Results are appended to results.csv next to this script. Standard library only.
"""
import csv
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
RESULTS = Path(__file__).with_name("results.csv")
NS = 1e9  # Ollama reports durations in nanoseconds

# A realistic "agent" system prompt: the finance tools the model will call later.
TOOLS_PROMPT = """You are a private finance assistant. Answer by calling exactly one tool.
Available tools:
1. spend_by_category(category: string, month: string "YYYY-MM") -> total spent in that category that month
2. find_transactions(merchant: string, month: string "YYYY-MM") -> list of matching transactions
3. monthly_summary(month: string "YYYY-MM") -> totals per category for the month
4. top_merchants(month: string "YYYY-MM", n: integer) -> the n merchants with the most spending
5. compare_months(category: string, month_a: string "YYYY-MM", month_b: string "YYYY-MM") -> spending in both months
Categories: Food, Groceries, Transport, Housing, Shopping, Bills, Entertainment, Other.
Rules: reply with ONLY a JSON object like {"tool": "<name>", "args": {...}}. No other text.
"""

PROMPTS = [
    # id, prompt text, checker (returns True/False/None)
    ("short", "Explain what a budget category is in two sentences.", None),
    ("categorize",
     "Categorize this bank transaction: 'STARBUCKS #1234 TAMPA FL $6.45'. "
     "Reply with exactly one word from: Food, Groceries, Transport, Housing, Shopping, Bills, Entertainment, Other.",
     lambda out: out.strip().strip(".").lower() == "food"),
    ("tool_call", TOOLS_PROMPT + "\nUser: How much did I spend on groceries in October 2026?",
     lambda out: _tool_is(out, "spend_by_category")),
    # Same long prefix again, right after: shows whether the prompt cache cuts the reading time.
    ("tool_call_repeat", TOOLS_PROMPT + "\nUser: How much did I spend on groceries in October 2026?",
     lambda out: _tool_is(out, "spend_by_category")),
]


def _tool_is(out, name):
    try:
        start, end = out.index("{"), out.rindex("}") + 1
        return json.loads(out[start:end]).get("tool") == name
    except (ValueError, json.JSONDecodeError, AttributeError):
        return False  # malformed output counts as a miss


def generate(model, prompt):
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0, "num_predict": 120},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read())


def rate(tokens, seconds):
    return round(tokens / seconds, 2) if seconds else 0.0


def main():
    models = sys.argv[1:] or ["llama3.2:3b"]
    new_file = not RESULTS.exists()
    with RESULTS.open("a", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["timestamp", "model", "prompt_id", "prompt_tokens", "prompt_eval_s",
                             "prompt_tok_per_s", "output_tokens", "eval_s", "output_tok_per_s",
                             "load_s", "total_s", "correct", "output_preview"])
        for model in models:
            print(f"\n=== {model} ===  (warming up...)")
            generate(model, "Hi")  # load the model so load time doesn't skew the first test
            print(f"{'prompt':<18}{'in tok':>7}{'read/s':>8}{'out tok':>8}{'write/s':>8}{'total s':>9}  correct")
            for pid, prompt, check in PROMPTS:
                r = generate(model, prompt)
                out = r.get("response", "")
                p_tok, p_s = r.get("prompt_eval_count", 0), r.get("prompt_eval_duration", 0) / NS
                o_tok, o_s = r.get("eval_count", 0), r.get("eval_duration", 0) / NS
                total, load = r.get("total_duration", 0) / NS, r.get("load_duration", 0) / NS
                correct = "" if check is None else check(out)
                print(f"{pid:<18}{p_tok:>7}{rate(p_tok, p_s):>8}{o_tok:>8}{rate(o_tok, o_s):>8}{total:>9.1f}  {correct}")
                writer.writerow([datetime.now().isoformat(timespec="seconds"), model, pid, p_tok, round(p_s, 2),
                                 rate(p_tok, p_s), o_tok, round(o_s, 2), rate(o_tok, o_s), round(load, 2),
                                 round(total, 2), correct, out.replace("\n", " ")[:80]])
                f.flush()
    print(f"\nSaved to {RESULTS}")


if __name__ == "__main__":
    main()
