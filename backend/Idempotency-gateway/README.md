# Idempotency-gateway
# Idempotency Gateway — Pay-Once Protocol

A FastAPI service that guarantees payment requests are processed **exactly once**, no matter how many times a client retries. Built for FinSafe Transactions Ltd.

---

## Architecture Diagram

The following sequence diagram covers all four scenarios handled by the gateway.

```
  Client              Gateway (FastAPI)            Store + AI
    │                       │                          │
    │  ① First request      │                          │
    │──POST /process-payment─▶                          │
    │   Idempotency-Key: K  │──── key exists? ─────────▶
    │                       │◀─── 404 not found ───────│
    │                       │  set status="processing" │
    │                       │──── AI fraud score ──────▶
    │                       │◀─── score: 0.08, safe ───│
    │                       │  [~2s processing delay]  │
    │                       │──── save result ─────────▶
    │◀── 201 Created ───────│                          │
    │    "Charged 100 GHS"  │                          │
    │                       │                          │
    │  ② Duplicate request  │                          │
    │──POST /process-payment─▶                          │
    │   same key + body     │──── key exists? ─────────▶
    │                       │◀─── 200 done, hash match ─│
    │◀── 201 + X-Cache-Hit──│                          │
    │    (no processing)    │                          │
    │                       │                          │
    │  ③ Conflict           │                          │
    │──POST /process-payment─▶                          │
    │   same key, amount=500│──── key exists? ─────────▶
    │                       │◀─── 200 done, hash mismatch│
    │◀── 422 Unprocessable ─│                          │
    │                       │                          │
    │  ④ Race condition     │                          │
    │  Request A ──────────▶ acquires asyncio.Lock     │
    │  Request B ──────────▶ blocks on Lock            │
    │                       │  A processes + saves      │
    │                       │  A releases Lock          │
    │                       │  B wakes, finds A's result│
    │◀── B returns A's resp ─│  X-Cache-Hit: true       │
```

---

## Setup Instructions

### Prerequisites

- Python 3.11+
- An Anthropic API key (optional — the AI fraud check degrades gracefully without it)

### 1. Clone and install

```bash
git clone https://github.com/<your-username>/idempotency-gateway.git
cd idempotency-gateway
mkdir app

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

### 3. Run

```bash
uvicorn app.main:app --reload
```

Server starts at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

---

## API Documentation

### POST `/process-payment`

Process a payment. Idempotent — identical requests with the same key return the cached response without re-processing.

**Headers**

| Header | Required | Description |
|--------|----------|-------------|
| `Idempotency-Key` | ✅ | A unique string per payment attempt (UUID recommended) |
| `Content-Type` | ✅ | `application/json` |

**Request body**

```json
{
  "amount": 100,
  "currency": "GHS"
}
```

**Responses**

| Scenario | Status | Body |
|----------|--------|------|
| First request (success) | `201 Created` | `{"status":"success","message":"Charged 100 GHS","fraud_check":{...}}` |
| Duplicate request | `201 Created` + `X-Cache-Hit: true` | Same body as first response |
| Same key, different body | `422 Unprocessable Entity` | `{"detail":"Idempotency key already used for a different request body."}` |
| High-risk transaction | `402 Payment Required` | `{"error":"...","fraud_score":0.91,"flags":["unusually large amount"]}` |
| Missing/invalid amount | `400 Bad Request` | `{"detail":"amount must be a positive number"}` |

**Example — first request**

```bash
curl -X POST http://localhost:8000/process-payment \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: a1b2c3d4-e5f6-7890-abcd-ef1234567890" \
  -d '{"amount": 100, "currency": "GHS"}'
```

```json
{
  "status": "success",
  "message": "Charged 100 GHS",
  "idempotency_key": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "fraud_check": {
    "score": 0.05,
    "flags": [],
    "safe": true
  }
}
```

**Example — duplicate request (same key)**

```bash
curl -X POST http://localhost:8000/process-payment \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: a1b2c3d4-e5f6-7890-abcd-ef1234567890" \
  -d '{"amount": 100, "currency": "GHS"}'
```

Returns the same `201` body instantly, with `X-Cache-Hit: true` in response headers.

**Example — conflict**

```bash
curl -X POST http://localhost:8000/process-payment \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: a1b2c3d4-e5f6-7890-abcd-ef1234567890" \
  -d '{"amount": 500, "currency": "GHS"}'
```

```json
{"detail": "Idempotency key already used for a different request body."}
```

---

### GET `/health`

Returns server status and number of active idempotency keys currently in memory.

```bash
curl http://localhost:8000/health
```

```json
{"status": "ok", "active_keys": 3}
```

---

## Design Decisions

### Why `asyncio.Lock` per key?

The bonus story requires that two simultaneous identical requests do not double-process. A global lock would serialize all requests. A per-key lock means only requests sharing the same `Idempotency-Key` block on each other — all other keys proceed in parallel. This gives correct isolation at zero throughput cost for unrelated payments.

### Why in-memory dict?

The spec explicitly lists a native dictionary as an acceptable store. It requires zero infrastructure, lets reviewers `git clone` → `pip install` → `uvicorn` with no external services. For production you would swap `store` for a Redis hash with atomic `SET NX PX` operations, preserving the same API contract.

### Why SHA-256 for body comparison?

Storing the full JSON body for every key wastes memory and makes comparison O(n). A SHA-256 hash is 64 bytes, O(1) to compare, and collision probability is negligible for payment payloads. `json.dumps(sort_keys=True)` ensures `{"amount":100,"currency":"GHS"}` and `{"currency":"GHS","amount":100}` hash identically.

### Why TTL on keys?

Without expiry, the in-memory store grows forever. A configurable `IDEMPOTENCY_TTL` (default 24 hours) matches Stripe's own idempotency key policy and prevents memory leaks in long-running deployments.

---

## Developer's Choice Feature: AI-Powered Fraud Detection

**What it does:** Before processing any new payment, the gateway calls Claude (via the Anthropic API) to score the transaction for anomalies. The model considers the amount, currency, and the structure of the idempotency key to produce:

- `score` — a float from 0.0 (safe) to 1.0 (very suspicious)
- `flags` — a list of human-readable anomaly descriptions
- `safe` — a boolean; `false` triggers a `402` rejection before any charge occurs

**Why this matters for a Fintech company:**

Rule-based fraud systems (block amounts > X, flag currencies Y and Z) are static and easy to circumvent. An LLM-based check can reason about combinations of signals that no hard-coded rule would catch, and it can explain its reasoning in the `flags` field — something auditors and compliance teams can actually read.

**Graceful degradation:** If the Anthropic API key is absent or the call times out, the fraud check returns `safe: true` and the payment proceeds normally. The AI layer is advisory, never a single point of failure.

**How to test it:** Set your `ANTHROPIC_API_KEY` in `.env` and send a suspiciously large amount:

```bash
curl -X POST http://localhost:8000/process-payment \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-fraud-$(date +%s)" \
  -d '{"amount": 999999, "currency": "XYZ"}'
```

You may receive a `402` with flags like `["unusually large amount", "unrecognised currency code"]`.

---

## Project Structure

```
idempotency-gateway/
├── app/
│   └── main.py          # FastAPI application (all logic)
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```
