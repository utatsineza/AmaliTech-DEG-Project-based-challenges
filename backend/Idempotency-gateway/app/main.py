import asyncio
import hashlib
import json
import time
import os
from typing import Any

import httpx
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI(title="Idempotency Gateway", version="1.0.0")

# ---------------------------------------------------------------------------------------
# In-memory store: { idempotency_key: { "status": "processing"|"done", "body_hash": str,
#                                        "response_body": dict, "status_code": int,
#                                        "created_at": float, "expires_at": float } }
# ---------------------------------------------------------------------------------------
store: dict[str, dict] = {}

# Per-key locks to handle race conditions (bonus story)
_locks: dict[str, asyncio.Lock] = {}
_locks_meta_lock = asyncio.Lock()   # protects _locks dict creation

TTL_SECONDS = int(os.getenv("IDEMPOTENCY_TTL", 86400))   # 24 h default


def _hash_body(body: dict) -> str:
    """SHA-256 of the canonical JSON body for conflict detection."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _get_lock(key: str) -> asyncio.Lock:
    async with _locks_meta_lock:
        if key not in _locks:
            _locks[key] = asyncio.Lock()
        return _locks[key]


async def _ai_fraud_score(amount: float, currency: str, idempotency_key: str) -> dict:
    """
    Developer's Choice Feature: AI-powered fraud detection.
    Calls Claude to score the transaction for anomalies.
    Returns a dict with { score: float, flags: list[str], safe: bool }.
    Falls back gracefully if the API is unavailable.
    """
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        return {"score": 0.0, "flags": [], "safe": True, "note": "AI check skipped (no API key)"}

    prompt = (
        f"You are a payment fraud detection engine. Evaluate this transaction:\n"
        f"  amount: {amount}\n"
        f"  currency: {currency}\n"
        f"  idempotency_key: {idempotency_key}\n\n"
        f"Reply ONLY with a JSON object (no markdown) with these fields:\n"
        f"  score: float 0.0–1.0 (0=safe, 1=very suspicious)\n"
        f"  flags: list of short strings describing any anomalies\n"
        f"  safe: bool (true if score < 0.7)\n"
        f"Base your score on: unusually large amount, suspicious currency patterns, "
        f"key looks like a random retry vs a structured ID. Be concise."
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 256,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            text = resp.json()["content"][0]["text"].strip()
            # Strip accidental markdown fences
            text = text.replace("```json", "").replace("```", "").strip()
            return json.loads(text)
    except Exception as exc:
        # Never block a payment because the AI check failed
        return {"score": 0.0, "flags": [f"ai_check_error: {str(exc)[:80]}"], "safe": True}


def _is_expired(record: dict) -> bool:
    return time.time() > record.get("expires_at", float("inf"))


# ---------------------------------------------------------------------------
# Cleanup helper — removes expired keys (runs lazily on each request)
# ---------------------------------------------------------------------------
def _purge_expired():
    expired = [k for k, v in store.items() if _is_expired(v)]
    for k in expired:
        store.pop(k, None)
        _locks.pop(k, None)


# ---------------------------------------------------------------------------
# POST /process-payment
# ---------------------------------------------------------------------------
@app.post("/process-payment")
async def process_payment(
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    _purge_expired()

    body: dict = await request.json()
    body_hash = _hash_body(body)
    amount = body.get("amount")
    currency = body.get("currency", "")

    # Basic input validation
    if amount is None or not isinstance(amount, (int, float)) or amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be a positive number")
    if not currency:
        raise HTTPException(status_code=400, detail="currency is required")

    lock = await _get_lock(idempotency_key)

    async with lock:
        # ---- Check store while holding the lock ----
        existing = store.get(idempotency_key)

        if existing and not _is_expired(existing):
            # User Story 2: duplicate request, same body
            if existing["body_hash"] == body_hash:
                response_body = existing["response_body"]
                status_code = existing["status_code"]
                return JSONResponse(
                    content=response_body,
                    status_code=status_code,
                    headers={"X-Cache-Hit": "true"},
                )
            # User Story 3: same key, different body → conflict
            else:
                raise HTTPException(
                    status_code=422,
                    detail="Idempotency key already used for a different request body.",
                )

        # ---- Mark as "processing" before releasing nothing (we still hold lock) ----
        store[idempotency_key] = {
            "status": "processing",
            "body_hash": body_hash,
            "response_body": None,
            "status_code": None,
            "created_at": time.time(),
            "expires_at": time.time() + TTL_SECONDS,
        }

        # ---- Developer's Choice: AI fraud scoring ----
        fraud = await _ai_fraud_score(amount, currency, idempotency_key)

        if not fraud.get("safe", True):
            # High-risk transaction — reject and clean up the key
            store.pop(idempotency_key, None)
            return JSONResponse(
                status_code=402,
                content={
                    "error": "Transaction flagged as high-risk by fraud detection.",
                    "fraud_score": fraud.get("score"),
                    "flags": fraud.get("flags", []),
                },
            )

        # ---- Simulate payment processing (2-second delay) ----
        await asyncio.sleep(2)

        response_body: dict[str, Any] = {
            "status": "success",
            "message": f"Charged {amount} {currency}",
            "idempotency_key": idempotency_key,
            "fraud_check": fraud,
        }
        status_code = 201

        # ---- Persist result ----
        store[idempotency_key].update({
            "status": "done",
            "response_body": response_body,
            "status_code": status_code,
        })

    return JSONResponse(content=response_body, status_code=status_code)


# ---------------------------------------------------------------------------
# GET /health  (bonus: simple health check)
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    active = sum(1 for v in store.values() if not _is_expired(v))
    return {"status": "ok", "active_keys": active}