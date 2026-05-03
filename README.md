<<<<<<< HEAD
# Vera Bot — magicpin AI Challenge Submission
=======
# Vera Bot 
>>>>>>> 4c443831d964e0c88da1fe675a2612684232c547

## Approach

**Model**: Claude Sonnet 4 (`claude-sonnet-4-20250514`) at `temperature=0` for determinism.

**Architecture**: A FastAPI server exposing all 5 required endpoints (`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`) with an in-memory context store and a Claude-powered message composer.

### Key design decisions

**1. Trigger-kind routing with tailored guidance**  
Every trigger kind (research_digest, perf_dip, competitor_opened, recall_due, etc.) gets a specific composition prompt injected into the Claude call. This avoids one-size-fits-all prompting and ensures the "why now" is always explicit in the output. Dentists get clinical-peer tone guidance; salons get warm-local; pharmacies get utility-first.

**2. Four-context injection into a single LLM call**  
All four context layers (category, merchant, trigger, customer) are serialized into a structured prompt block — including peer stats, delta_7d performance, active offers, signals, digest items, seasonal beats, and trend signals. Claude reasons over the full context rather than receiving pre-filtered data.

**3. Auto-reply detection (pattern-based, fast)**  
Runs before any LLM call in `/v1/reply`. A list of known auto-reply phrases (WhatsApp Business templates) catches them in O(1). On first detection: one re-engagement attempt. On second: graceful exit. No LLM tokens wasted.

**4. Intent transition without re-qualifying**  
When a merchant replies with an explicit acceptance ("yes", "haan", "karo", "go ahead", "bilkul", etc.), the handler switches from pitch mode to action mode immediately — using an action-specific prompt variant that drafts the deliverable rather than asking another qualifying question. This directly addresses Pattern D in the brief.

**5. Anti-repetition tracking**  
Each conversation state tracks a set of `sent_bodies`. Before returning a reply, the bot checks for verbatim repeats and appends a variation token if the same message is about to be sent again.

**6. Suppression dedup**  
A global `sent_suppressions` set ensures each `suppression_key` fires only once per session. This prevents the same research digest or milestone message from going to the same merchant twice in one test window.

### Tradeoffs made

- **In-memory store**: Fast, no infra overhead. Would use Redis for production persistence across restarts.
- **Single Claude call per composition**: Could be improved with retrieval (embed digest items, pull top-3 relevant for the trigger) for longer category digests. Kept simple for latency.
- **Pattern-based auto-reply detection**: Works for known WA Business templates; a classification model would generalize better to novel auto-replies.
- **`temperature=0`**: Deterministic but may produce the same phrasing across similar merchants. A small temperature (0.1) with a fixed seed would be preferable for production variety.

### What additional context would have helped most

1. **Real conversation history from production Vera** — even 10 anonymized merchant threads per category would dramatically improve the multi-turn handler's naturalness.
2. **Language detection per message** — the current bot infers from `identity.languages` but real merchants switch mid-conversation. Per-turn language detection would improve code-mix accuracy.
3. **Offer click-through data** — knowing which offer copy actually drove CTR for similar merchants would let the composer pick the proven framing rather than the theoretically correct one.
4. **Merchant segment data** — single practitioner vs. chain vs. franchise affects tone and offer structure significantly. One flag in MerchantContext would improve category fit.

## Running locally

```bash
# Install dependencies
pip install fastapi uvicorn pydantic

# Set API key
export ANTHROPIC_API_KEY=your_key_here

# Start bot
python bot.py
# → runs on http://localhost:8080

# Run judge simulator
python judge_simulator.py
```

## Files

| File | Purpose |
|---|---|
| `bot.py` | Main FastAPI server — all 5 endpoints, Claude composer, state management |
| `conversation_handlers.py` | Multi-turn logic: auto-reply detection, intent transition, hostile handling |
| `submission.jsonl` | 30 composed messages for the canonical test pairs |
| `dataset/expanded/` | Full generated dataset (50 merchants, 200 customers, 100 triggers) |
| `generate_submission.py` | Script used to generate submission.jsonl (requires ANTHROPIC_API_KEY) |
