#!/usr/bin/env python3
"""
Vera Bot — magicpin AI Challenge Submission
==========================================
A production-grade merchant AI assistant that composes highly specific,
context-aware WhatsApp messages for Indian merchants via the Anthropic API.

Architecture:
- FastAPI server exposing 5 required endpoints
- In-memory context store (idempotent by scope+version)
- Claude-powered message composer with trigger-kind routing
- Multi-turn conversation state tracking
- Auto-reply detection
- Intent transition handling
"""

import os
import time
import json
import uuid
import re
from datetime import datetime, timezone
from typing import Any, Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ─── Anthropic client ────────────────────────────────────────────────────────
import urllib.request
import urllib.error


def call_claude(system_prompt: str, user_prompt: str, max_tokens: int = 1000) -> str:
    """Call Claude API directly via HTTP."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": max_tokens,
        "temperature": 0,  # deterministic
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}]
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read())
    return data["content"][0]["text"]


# ─── App + State ─────────────────────────────────────────────────────────────
app = FastAPI(title="Vera Bot")
START_TIME = time.time()

# (scope, context_id) -> {version, payload}
contexts: dict[tuple[str, str], dict] = {}

# conversation_id -> {merchant_id, customer_id, turns: [], sent_bodies: set}
conversations: dict[str, dict] = {}

# suppression_key -> bool (already sent)
sent_suppressions: set[str] = set()


# ─── Pydantic models ─────────────────────────────────────────────────────────
class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


# ─── Helpers ─────────────────────────────────────────────────────────────────
def get_context(scope: str, context_id: str) -> Optional[dict]:
    entry = contexts.get((scope, context_id))
    return entry["payload"] if entry else None


def context_counts() -> dict:
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _) in contexts:
        if scope in counts:
            counts[scope] += 1
    return counts


def is_auto_reply(message: str) -> bool:
    """Detect WhatsApp Business auto-replies."""
    auto_reply_signals = [
        "thank you for contacting",
        "aapki jaankari ke liye",
        "bahut-bahut shukriya",
        "automated assistant",
        "main ek automated",
        "yeh ek automated",
        "we will get back",
        "hum aapko jald",
        "out of office",
        "currently unavailable",
        "please leave a message",
        "this is an automated",
        "auto-reply",
        "bot hai",
    ]
    msg_lower = message.lower()
    return any(signal in msg_lower for signal in auto_reply_signals)


def is_explicit_accept(message: str) -> bool:
    """Detect merchant saying yes/go ahead."""
    accept_signals = [
        "yes", "haan", "ha ", "ha,", "chalega", "theek", "okay", "ok",
        "go ahead", "karo", "kar do", "send karo", "done", "sure",
        "let's do", "lets do", "bahut accha", "bilkul", "zaroor",
        "proceed", "start", "shuru"
    ]
    msg_lower = message.lower().strip()
    return any(signal in msg_lower for signal in accept_signals)


def is_exit_signal(message: str) -> bool:
    """Detect not-interested signals."""
    exit_signals = [
        "not interested", "nahi chahiye", "nahi", "mat bhejo",
        "stop", "unsubscribe", "band karo", "baat nahi karni",
        "don't contact", "do not contact", "remove me", "no thanks",
        "nahi chahiye", "hata do", "block",
    ]
    msg_lower = message.lower()
    return any(signal in msg_lower for signal in exit_signals)


# ─── Core Composer ───────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Vera, magicpin's AI assistant for merchant growth in India.
You compose WhatsApp messages to merchants or their customers — specific, useful, and easy to reply to.

RULES (non-negotiable):
1. Anchor on a REAL fact from the context (number, date, headline, peer stat, offer price). No generic "increase your sales".
2. ONE clear CTA at the end: binary YES/NO for action triggers; open question or none for info triggers.
3. Voice match: clinical+peer for dentists; energetic for gyms; warm+local for restaurants/salons; utility for pharmacies.
4. Hindi-English code-mix is natural and preferred when merchant language includes "hi". Match merchant's vibe.
5. No preambles ("I hope you're doing well"). Get to the point immediately.
6. No fake data. If a fact isn't in the context, don't invent it.
7. Keep it short — 3-5 sentences max for merchant messages; 2-3 for customer messages.
8. Anti-patterns: "Flat X% off" (use "ServiceName @ ₹price"), multiple CTAs, promotional ALL CAPS, re-introducing yourself.
9. For customer messages (send_as=merchant_on_behalf): sent FROM the merchant TO their customer. Sound like the clinic/salon/gym talking.

COMPULSION LEVERS (use 1-2 per message):
- Specificity: "2,100-patient JIDA trial showed 38% improvement"
- Loss aversion: "you're missing 190 searches in your area"  
- Social proof: "3 dentists in Lajpat Nagar did this last month"
- Effort externalization: "I've drafted it — just say go"
- Curiosity: "Want to see the full breakdown?"
- Single binary: Reply YES / STOP

OUTPUT FORMAT (JSON only, no markdown):
{
  "body": "the WhatsApp message text",
  "cta": "open_ended" | "binary_yes_stop" | "none",
  "send_as": "vera" | "merchant_on_behalf",
  "suppression_key": "kind:merchant_id:period",
  "rationale": "1-2 sentences: what lever used, why this message now"
}"""


def build_composer_prompt(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict] = None,
    conversation_history: Optional[list] = None
) -> str:
    """Build the user-facing prompt for Claude."""
    
    # Extract key facts
    trigger_kind = trigger.get("kind", "unknown")
    merchant_name = merchant.get("identity", {}).get("name", "merchant")
    owner_name = merchant.get("identity", {}).get("owner_first_name", "")
    city = merchant.get("identity", {}).get("city", "")
    locality = merchant.get("identity", {}).get("locality", "")
    languages = merchant.get("identity", {}).get("languages", ["en"])
    cat_slug = category.get("slug", "")
    
    # Performance
    perf = merchant.get("performance", {})
    views = perf.get("views", 0)
    calls = perf.get("calls", 0)
    ctr = perf.get("ctr", 0)
    delta_7d = perf.get("delta_7d", {})
    
    # Category facts
    peer_stats = category.get("peer_stats", {})
    peer_ctr = peer_stats.get("avg_ctr", 0)
    digest = category.get("digest", [])
    offers = merchant.get("offers", [])
    active_offers = [o for o in offers if o.get("status") == "active"]
    signals = merchant.get("signals", [])
    conv_hist = merchant.get("conversation_history", [])
    
    # Trigger payload
    payload = trigger.get("payload", {})
    
    # Language hint
    uses_hindi = "hi" in languages or "hi-en mix" in [l for l in languages]
    lang_hint = "Use natural Hindi-English code-mix (like 'Aapka CTR 2.1% hai, peer median 3%')." if uses_hindi else "Use English."
    
    parts = [
        f"TRIGGER KIND: {trigger_kind}",
        f"TRIGGER URGENCY: {trigger.get('urgency', 1)}/5",
        f"TRIGGER PAYLOAD: {json.dumps(payload, ensure_ascii=False)}",
        f"",
        f"MERCHANT: {merchant_name} ({cat_slug}) | {locality}, {city}",
        f"OWNER: {owner_name}",
        f"SUBSCRIPTION: {merchant.get('subscription', {}).get('status', 'unknown')}, {merchant.get('subscription', {}).get('days_remaining', 0)} days remaining",
        f"PERFORMANCE (30d): views={views}, calls={calls}, CTR={ctr:.1%}",
        f"PEER MEDIAN CTR: {peer_ctr:.1%} (merchant is {'BELOW' if ctr < peer_ctr else 'ABOVE'} peer)",
        f"7d DELTA: views {delta_7d.get('views_pct', 0):+.0%}, calls {delta_7d.get('calls_pct', 0):+.0%}",
        f"ACTIVE OFFERS: {json.dumps([o['title'] for o in active_offers], ensure_ascii=False)}",
        f"MERCHANT SIGNALS: {signals}",
        f"CUSTOMER AGGREGATE: {json.dumps(merchant.get('customer_aggregate', {}), ensure_ascii=False)}",
        f"LANGUAGE: {lang_hint}",
    ]
    
    # Recent conversation history
    if conv_hist:
        recent = conv_hist[-3:]
        parts.append(f"\nRECENT VERA CONVERSATIONS: {json.dumps(recent, ensure_ascii=False)}")
    
    # Digest items (most relevant)
    if digest:
        top_digest = digest[:3]
        parts.append(f"\nCATEGORY DIGEST (research/news): {json.dumps(top_digest, ensure_ascii=False)}")
    
    # Seasonal + trend signals
    seasonal = category.get("seasonal_beats", [])
    trends = category.get("trend_signals", [])
    if seasonal:
        parts.append(f"SEASONAL BEATS: {json.dumps(seasonal, ensure_ascii=False)}")
    if trends:
        parts.append(f"TREND SIGNALS: {json.dumps(trends, ensure_ascii=False)}")
    
    # Offer catalog from category
    cat_offers = category.get("offer_catalog", [])
    if cat_offers:
        parts.append(f"CATEGORY OFFER CATALOG: {json.dumps([o['title'] for o in cat_offers[:5]], ensure_ascii=False)}")
    
    # Customer context (if present)
    if customer:
        parts.append(f"\nCUSTOMER CONTEXT:")
        parts.append(f"  Name: {customer.get('identity', {}).get('name', 'Customer')}")
        parts.append(f"  Language: {customer.get('identity', {}).get('language_pref', 'en')}")
        parts.append(f"  State: {customer.get('state', 'unknown')}")
        parts.append(f"  Last visit: {customer.get('relationship', {}).get('last_visit', 'unknown')}")
        parts.append(f"  Services received: {customer.get('relationship', {}).get('services_received', [])}")
        parts.append(f"  Preferences: {json.dumps(customer.get('preferences', {}), ensure_ascii=False)}")
        parts.append(f"  Consent scope: {customer.get('consent', {}).get('scope', [])}")
        parts.append(f"  NOTE: This is a customer-facing message. send_as=merchant_on_behalf. Sound like {merchant_name} talking to their customer.")
    
    # Multi-turn context
    if conversation_history:
        parts.append(f"\nONGOING CONVERSATION (previous turns):")
        for turn in conversation_history[-5:]:
            parts.append(f"  [{turn['from']}]: {turn['msg'][:200]}")
        parts.append("Craft a FOLLOW-UP that advances the conversation without repeating what was already said.")
    
    # Trigger-specific guidance
    kind_guidance = {
        "research_digest": "Lead with the specific research finding. Offer to do work for the merchant (draft content, pull the abstract). Use clinical curiosity.",
        "regulation_change": "Frame as 'need to know' for their practice. Cite the specific rule/deadline. Urgency is real.",
        "recall_due": "This is a customer recall message. Be warm, personal, mention the specific service due and available slots.",
        "perf_dip": "Frame as 'I noticed, thought you should know'. Show the specific number drop. Offer a concrete fix.",
        "perf_spike": "Celebrate the win briefly, then pivot to 'let's lock it in' with a specific action.",
        "competitor_opened": "Acknowledge the threat matter-of-factly. Show what they have vs the merchant's strengths. Offer a defensive action.",
        "festival_upcoming": "Frame as 'prep window open'. Specific festival + category-relevant angle. Offer to set up the campaign.",
        "milestone_reached": "Brief celebration, then immediate 'let's convert this momentum' pivot.",
        "dormant_with_vera": "Re-engage with something genuinely useful (new trend, peer stat). Don't mention the dormancy explicitly.",
        "renewal_due": "Show the value (their numbers since joining). Frame renewal as protecting that momentum.",
        "review_theme_emerged": "Show the specific pattern (N reviews, the quote). Offer to respond or address the issue.",
        "curious_ask_due": "Ask a genuinely interesting business question. Frame it as market research you're curious about.",
        "winback_eligible": "Show what they're missing (lapsed customer count, missed searches). Loss aversion angle.",
        "active_planning_intent": "Merchant already said yes to the idea. MOVE TO ACTION immediately. Draft it for them.",
        "ipl_match_today": "Hyper-local angle (venue, team, match time). Offer a same-day promotion draft.",
        "seasonal_perf_dip": "If expected seasonal, frame as 'normal but let's get ahead of it'. Show the comparison.",
        "customer_lapsed_soft": "Warm re-engagement from merchant to customer. Personal touch, specific service history.",
        "appointment_tomorrow": "Day-before reminder from merchant. Warm, brief, slot details.",
        "chronic_refill_due": "Pharmacy refill reminder. Include medication category (not name). Framed as care from the pharmacy.",
        "trial_followup": "Follow up on a trial service with a specific ask or next-step offer.",
        "unverified_gbp": "Show the cost of being unverified (searches they're invisible to). Offer to start the fix now.",
    }
    
    guidance = kind_guidance.get(trigger_kind, "Compose a relevant, specific message using facts from the context.")
    parts.append(f"\nTRIGGER-SPECIFIC GUIDANCE: {guidance}")
    
    default_sup = trigger_kind + ":" + merchant.get("merchant_id", "") + ":2026"
    parts.append(f"\nSUPPRESSION KEY to use: {trigger.get('suppression_key', default_sup)}")
    parts.append(f"\nReturn JSON only. No markdown fences. No extra text.")
    
    return "\n".join(parts)


def compose_message(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict] = None,
    conversation_history: Optional[list] = None
) -> dict:
    """Compose a message using Claude."""
    user_prompt = build_composer_prompt(category, merchant, trigger, customer, conversation_history)
    
    raw = call_claude(SYSTEM_PROMPT, user_prompt, max_tokens=800)
    
    # Strip any markdown fences
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"```(?:json)?\n?", "", raw).strip().rstrip("`").strip()
    
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON from the response
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            result = json.loads(match.group())
        else:
            # Fallback
            result = {
                "body": raw[:500],
                "cta": "open_ended",
                "send_as": "vera",
                "suppression_key": trigger.get("suppression_key", "fallback"),
                "rationale": "Fallback composition"
            }
    
    return result


def compose_reply(
    merchant_id: str,
    customer_id: Optional[str],
    message: str,
    conversation: dict,
    turn_number: int
) -> dict:
    """Compose a reply to a merchant/customer message."""
    
    # Load contexts
    merchant = None
    for (scope, cid), entry in contexts.items():
        if scope == "merchant" and cid == merchant_id:
            merchant = entry["payload"]
            break
    
    category = None
    if merchant:
        cat_slug = merchant.get("category_slug", "")
        category = get_context("category", cat_slug)
    
    customer = None
    if customer_id:
        customer = get_context("customer", customer_id)
    
    # Detect patterns
    if is_auto_reply(message):
        # Try once more with a direct question, then exit if it happens again
        auto_count = sum(1 for t in conversation.get("turns", []) 
                        if t.get("is_auto_reply"))
        if auto_count >= 2:
            return {
                "action": "end",
                "rationale": "Multiple auto-replies detected. Gracefully exiting — will reconnect with the merchant directly."
            }
        # First auto-reply: try a direct question
        if merchant:
            name = merchant.get("identity", {}).get("name", "")
            owner = merchant.get("identity", {}).get("owner_first_name", "")
            return {
                "action": "send",
                "body": f"Lagta hai yeh automated reply hai. Kya {owner or name} ko directly connect kar sakti hoon? Just reply 'yes' if you'd like to continue.",
                "cta": "binary_yes_stop",
                "rationale": "Auto-reply detected. Making one attempt to reach the actual owner before exiting."
            }
    
    if is_exit_signal(message):
        return {
            "action": "end",
            "rationale": "Merchant signaled not interested. Gracefully exiting — no further messages will be sent."
        }
    
    if is_explicit_accept(message):
        # Intent transition: merchant said yes, move to action immediately
        system = """You are Vera, magicpin's merchant AI. The merchant just accepted/said yes.
Move IMMEDIATELY to action — do NOT ask qualifying questions. Draft the thing, set up the campaign, or confirm the next concrete step.
Return JSON: {"action": "send", "body": "...", "cta": "open_ended"|"binary_yes_stop"|"none", "rationale": "..."}"""
        
        history_text = "\n".join([f"[{t['from']}]: {t['msg']}" for t in conversation.get("turns", [])[-6:]])
        merchant_info = json.dumps({
            "name": merchant.get("identity", {}).get("name") if merchant else "merchant",
            "category": merchant.get("category_slug") if merchant else "",
            "offers": [o["title"] for o in (merchant.get("offers", []) if merchant else []) if o.get("status") == "active"]
        }, ensure_ascii=False) if merchant else "{}"
        
        prompt = f"""Merchant just said: "{message}"

Previous conversation:
{history_text}

Merchant info: {merchant_info}

They said YES. Move to action immediately. Draft/confirm the specific next step.
Return JSON only."""
        
        raw = call_claude(system, prompt, max_tokens=500)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"```(?:json)?\n?", "", raw).strip().rstrip("`").strip()
        try:
            result = json.loads(raw)
            if "action" not in result:
                result["action"] = "send"
            return result
        except:
            pass
    
    # General reply composition
    system_reply = """You are Vera, magicpin's merchant AI assistant.
The merchant has replied to your previous message. Compose a natural, context-aware follow-up.

Rules:
- Never repeat what you already said verbatim
- If merchant is engaged, advance to the next logical step
- Keep it short (2-4 sentences)
- Match their language (Hindi-English mix if they used it)
- End with one clear action or question

Return JSON: {"action": "send"|"wait"|"end", "body": "...", "cta": "open_ended"|"binary_yes_stop"|"none", "rationale": "..."}
Use "end" if conversation is clearly concluded. Use "wait" with wait_seconds if merchant needs time."""

    history_text = "\n".join([f"[{t['from']}]: {t['msg']}" for t in conversation.get("turns", [])[-6:]])
    sent_bodies = list(conversation.get("sent_bodies", set()))[-3:]
    
    merchant_info = {}
    if merchant:
        merchant_info = {
            "name": merchant.get("identity", {}).get("name"),
            "category": merchant.get("category_slug"),
            "languages": merchant.get("identity", {}).get("languages", ["en"]),
            "active_offers": [o["title"] for o in merchant.get("offers", []) if o.get("status") == "active"]
        }
    
    prompt = f"""Previous conversation:
{history_text}

Merchant's latest message (turn {turn_number}): "{message}"

Merchant info: {json.dumps(merchant_info, ensure_ascii=False)}
Previously sent messages (don't repeat these): {sent_bodies}

Compose a follow-up. Return JSON only."""
    
    raw = call_claude(system_reply, prompt, max_tokens=500)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"```(?:json)?\n?", "", raw).strip().rstrip("`").strip()
    
    try:
        result = json.loads(raw)
        if "action" not in result:
            result["action"] = "send"
        return result
    except:
        return {
            "action": "send",
            "body": "Aapki baat samajh li. Kya aap chahenge main aage badhun?",
            "cta": "binary_yes_stop",
            "rationale": "Fallback reply"
        }


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": context_counts()
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Vera Prime",
        "team_members": ["Claude Sonnet"],
        "model": "claude-sonnet-4-20250514",
        "approach": "Trigger-kind routing + Claude composer with 4-context injection. Auto-reply detection, intent-transition handling, suppression dedup. Full multi-turn conversation state.",
        "contact_email": "team@example.com",
        "version": "1.0.0",
        "submitted_at": datetime.now(timezone.utc).isoformat()
    }


@app.post("/v1/context")
async def push_context(body: CtxBody):
    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return JSONResponse({
            "accepted": False,
            "reason": "stale_version",
            "current_version": cur["version"]
        }, status_code=409)
    
    valid_scopes = {"category", "merchant", "customer", "trigger"}
    if body.scope not in valid_scopes:
        return JSONResponse({
            "accepted": False,
            "reason": "invalid_scope",
            "details": f"scope must be one of {valid_scopes}"
        }, status_code=400)
    
    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": datetime.now(timezone.utc).isoformat()
    }


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    
    for trg_id in body.available_triggers:
        # Check suppression
        trg_entry = contexts.get(("trigger", trg_id))
        if not trg_entry:
            continue
        trg = trg_entry["payload"]
        
        sup_key = trg.get("suppression_key", trg_id)
        if sup_key in sent_suppressions:
            continue
        
        # Load merchant
        merchant_id = trg.get("merchant_id")
        if not merchant_id:
            continue
        merchant = get_context("merchant", merchant_id)
        if not merchant:
            continue
        
        # Load category
        cat_slug = merchant.get("category_slug", "")
        category = get_context("category", cat_slug)
        if not category:
            continue
        
        # Load customer (if applicable)
        customer_id = trg.get("customer_id")
        customer = get_context("customer", customer_id) if customer_id else None
        
        try:
            result = compose_message(category, merchant, trg, customer)
        except Exception as e:
            continue
        
        # Validate body not empty
        body_text = result.get("body", "").strip()
        if not body_text:
            continue
        
        # Mark suppression
        sent_suppressions.add(sup_key)
        
        # Create conversation
        conv_id = f"conv_{merchant_id}_{trg_id}_{uuid.uuid4().hex[:6]}"
        conversations[conv_id] = {
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "trigger_id": trg_id,
            "turns": [{"from": "vera", "msg": body_text}],
            "sent_bodies": {body_text},
            "auto_reply_count": 0,
        }
        
        action = {
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": result.get("send_as", "vera"),
            "trigger_id": trg_id,
            "template_name": f"vera_{trg.get('kind', 'generic')}_v1",
            "template_params": [
                merchant.get("identity", {}).get("name", ""),
                trg.get("kind", ""),
                body_text[:50]
            ],
            "body": body_text,
            "cta": result.get("cta", "open_ended"),
            "suppression_key": sup_key,
            "rationale": result.get("rationale", "")
        }
        actions.append(action)
        
        if len(actions) >= 20:
            break
    
    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conv = conversations.get(body.conversation_id)
    if not conv:
        # Create conversation if unknown
        conversations[body.conversation_id] = {
            "merchant_id": body.merchant_id,
            "customer_id": body.customer_id,
            "turns": [],
            "sent_bodies": set(),
            "auto_reply_count": 0,
        }
        conv = conversations[body.conversation_id]
    
    # Track auto-reply count
    is_auto = is_auto_reply(body.message)
    if is_auto:
        conv["auto_reply_count"] = conv.get("auto_reply_count", 0) + 1
    
    # Record turn
    conv["turns"].append({
        "from": body.from_role,
        "msg": body.message,
        "is_auto_reply": is_auto
    })
    
    # Generate response
    result = compose_reply(
        merchant_id=body.merchant_id or conv.get("merchant_id", ""),
        customer_id=body.customer_id or conv.get("customer_id"),
        message=body.message,
        conversation=conv,
        turn_number=body.turn_number
    )
    
    action = result.get("action", "send")
    
    if action == "send":
        resp_body = result.get("body", "")
        # Anti-repetition check
        if resp_body in conv.get("sent_bodies", set()):
            # Add a small variation
            resp_body = resp_body + " Kya aap chahenge main aage badhun?"
        
        conv["turns"].append({"from": "vera", "msg": resp_body})
        conv.setdefault("sent_bodies", set()).add(resp_body)
        
        return {
            "action": "send",
            "body": resp_body,
            "cta": result.get("cta", "open_ended"),
            "rationale": result.get("rationale", "")
        }
    elif action == "wait":
        return {
            "action": "wait",
            "wait_seconds": result.get("wait_seconds", 1800),
            "rationale": result.get("rationale", "Merchant needs time")
        }
    else:  # end
        return {
            "action": "end",
            "rationale": result.get("rationale", "Conversation concluded")
        }


@app.post("/v1/teardown")
async def teardown():
    """Optional: wipe state at end of test."""
    contexts.clear()
    conversations.clear()
    sent_suppressions.clear()
    return {"status": "wiped"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
