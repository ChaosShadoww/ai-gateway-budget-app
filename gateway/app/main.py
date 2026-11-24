"""Private LLM gateway: one endpoint in front of the local model.

- POST /v1/chat   chat with the local model; optional JSON-schema-enforced output with retries
- GET  /health    is the gateway up, and can it reach Ollama?
- GET  /stats     latency percentiles, success and retry rates from the request log

Privacy: the log stores timing and token counts only, never prompt or answer text.
"""
import json
import os
import sqlite3
import statistics
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from jsonschema import Draft202012Validator, SchemaError, ValidationError
from pydantic import BaseModel, Field

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "llama3.2:3b")
DB_PATH = os.getenv("DB_PATH", "/data/gateway.db")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "30"))
OLLAMA_TIMEOUT_S = float(os.getenv("OLLAMA_TIMEOUT_S", "300"))
NS = 1e9  # Ollama reports durations in nanoseconds


# ---------- request / response shapes ----------
class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1)
    model: Optional[str] = None
    json_schema: Optional[dict[str, Any]] = None  # if set, the reply must be JSON matching it
    max_tokens: int = Field(default=256, ge=1, le=2048)
    private: bool = True  # private requests never leave this machine


class ChatResponse(BaseModel):
    content: str
    parsed: Optional[Any] = None
    model: str
    route: str
    attempts: int
    timings: dict[str, int | float]


# ---------- request log (metadata only) ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, route TEXT, model TEXT, ok INTEGER, attempts INTEGER,
        prompt_tokens INTEGER, output_tokens INTEGER,
        prompt_s REAL, eval_s REAL, total_s REAL, error TEXT)""")
    # A table created by the guide's Milestone 6 has no "route" column: add it if missing.
    cols = [row[1] for row in conn.execute("PRAGMA table_info(requests)")]
    if "route" not in cols:
        conn.execute("ALTER TABLE requests ADD COLUMN route TEXT")
    return conn


def log_request(route, model, ok, attempts, t, error=None):
    with db() as conn:
        conn.execute(
            "INSERT INTO requests (ts, route, model, ok, attempts, prompt_tokens, output_tokens, "
            "prompt_s, eval_s, total_s, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), route, model, int(ok), attempts,
             int(t.get("prompt_tokens", 0)), int(t.get("output_tokens", 0)),
             t.get("prompt_s", 0.0), t.get("eval_s", 0.0), t.get("total_s", 0.0), error))


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db().close()
    app.state.http = httpx.AsyncClient(base_url=OLLAMA_URL, timeout=OLLAMA_TIMEOUT_S)
    yield
    await app.state.http.aclose()


app = FastAPI(title="Private LLM Gateway", lifespan=lifespan)


# ---------- rate limiting (sliding window per client) ----------
_hits: dict[str, deque] = defaultdict(deque)


def check_rate_limit(client: str):
    now = time.monotonic()
    q = _hits[client]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MIN:
        wait = max(1, int(60 - (now - q[0])) + 1)   # seconds until the oldest request leaves the window
        raise HTTPException(429, f"Rate limit: {RATE_LIMIT_PER_MIN} requests per minute. Try again in {wait} s.",
                            headers={"Retry-After": str(wait)})
    q.append(now)


# ---------- routing ----------
def choose_route(req: ChatRequest) -> str:
    # For now everything runs locally. Hosted-model routing comes later,
    # and private requests will always stay local.
    return "local"


# ---------- Ollama call ----------
async def call_ollama(model: str, messages: list[dict], schema: Optional[dict], max_tokens: int) -> dict:
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": "24h",
        "options": {"temperature": 0, "num_predict": max_tokens},
    }
    if schema:
        body["format"] = schema  # Ollama constrains generation to this JSON schema
    try:
        r = await app.state.http.post("/api/chat", json=body)
    except httpx.ConnectError:
        raise HTTPException(503, "Can't reach Ollama. Is the ollama container running?")
    except httpx.TimeoutException:
        raise HTTPException(504, f"Ollama took longer than {OLLAMA_TIMEOUT_S:.0f} s.")
    if r.status_code != 200:
        raise HTTPException(502, f"Ollama error {r.status_code}: {r.text[:200]}")
    return r.json()


# ---------- endpoints ----------
@app.post("/v1/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    check_rate_limit(request.client.host if request.client else "unknown")
    route = choose_route(req)
    model = req.model or DEFAULT_MODEL

    validator = None
    if req.json_schema is not None:
        try:
            Draft202012Validator.check_schema(req.json_schema)
        except SchemaError as e:
            raise HTTPException(400, f"json_schema is not a valid JSON Schema: {e.message}")
        validator = Draft202012Validator(req.json_schema)

    messages = [m.model_dump() for m in req.messages]
    t = {"prompt_tokens": 0, "output_tokens": 0, "prompt_s": 0.0, "eval_s": 0.0, "load_s": 0.0}
    start = time.perf_counter()
    content, parsed, last_error = "", None, None

    for attempt in range(1, MAX_RETRIES + 2):
        try:
            r = await call_ollama(model, messages, req.json_schema, req.max_tokens)
        except HTTPException as e:
            t["total_s"] = round(time.perf_counter() - start, 3)
            log_request(route, model, False, attempt, t, error=str(e.detail)[:200])
            raise
        content = r.get("message", {}).get("content", "")
        t["prompt_tokens"] += r.get("prompt_eval_count", 0)
        t["output_tokens"] += r.get("eval_count", 0)
        t["prompt_s"] += r.get("prompt_eval_duration", 0) / NS
        t["eval_s"] += r.get("eval_duration", 0) / NS
        t["load_s"] += r.get("load_duration", 0) / NS

        if validator is None:
            last_error = None
            break
        try:
            parsed = json.loads(content)
            validator.validate(parsed)
            last_error = None
            break
        except json.JSONDecodeError as e:
            last_error = f"not valid JSON ({e.msg})"
        except ValidationError as e:
            last_error = f"doesn't match the schema ({e.message})"
        parsed = None
        # Re-ask: keep the original prefix (so the prompt cache still helps) and add the correction.
        messages = messages + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": f"That reply was invalid: {last_error}. "
                                        "Reply again with ONLY a JSON object that matches the schema."},
        ]

    t = {k: (v if isinstance(v, int) else round(v, 3)) for k, v in t.items()}
    t["total_s"] = round(time.perf_counter() - start, 3)
    if last_error:
        log_request(route, model, False, attempt, t, error=last_error[:200])
        raise HTTPException(502, {"error": f"Model output still invalid after {attempt} attempts: {last_error}",
                                  "last_output": content[:500]})
    log_request(route, model, True, attempt, t)
    return ChatResponse(content=content, parsed=parsed, model=model, route=route, attempts=attempt, timings=t)


@app.get("/health")
async def health():
    try:
        r = await app.state.http.get("/api/tags", timeout=5)
        models = [m["name"] for m in r.json().get("models", [])]
    except Exception:
        raise HTTPException(503, "Gateway is up, but it can't reach Ollama.")
    return {"status": "ok", "ollama": True, "default_model": DEFAULT_MODEL, "models": models}


@app.get("/stats")
def stats(last: int = 500):
    with db() as conn:
        rows = conn.execute(
            "SELECT ok, attempts, total_s, prompt_tokens FROM requests ORDER BY id DESC LIMIT ?",
            (last,)).fetchall()
    if not rows:
        return {"requests": 0}
    totals = sorted(r[2] for r in rows if r[0])

    def pct(p):
        if not totals:
            return None
        if len(totals) == 1:
            return totals[0]
        return round(statistics.quantiles(totals, n=100, method="inclusive")[p - 1], 3)

    return {
        "requests": len(rows),
        "success_rate": round(sum(r[0] for r in rows) / len(rows), 3),
        "retry_rate": round(sum(1 for r in rows if r[1] > 1) / len(rows), 3),
        "latency_s": {"p50": pct(50), "p95": pct(95), "max": totals[-1] if totals else None},
        "avg_prompt_tokens": round(sum(r[3] or 0 for r in rows) / len(rows), 1),
    }