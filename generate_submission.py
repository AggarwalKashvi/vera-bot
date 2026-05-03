#!/usr/bin/env python3
"""Generate submission.jsonl — 30 composed messages for the canonical test pairs."""

import json
import os
import re
import sys
import time
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent))

EXPANDED = Path(__file__).parent / "dataset" / "expanded"


def load_json(path):
    with open(path) as f:
        return json.load(f)


def call_claude(system_prompt: str, user_prompt: str, max_tokens: int = 800) -> str:
    import urllib.request
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": max_tokens,
        "temperature": 0,
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


SYSTEM_PROMPT = """You are Vera, magicpin's AI assistant for merchant growth in India.
You compose WhatsApp messages to merchants or their customers — specific, useful, and easy to reply to.

RULES (non-negotiable):
1. Anchor on a REAL fact from the context (number, date, headline, peer stat, offer price). No generic "increase your sales".
2. ONE clear CTA at the end: binary YES/NO for action triggers; open question or none for info triggers.
3. Voice match: clinical+peer for dentists; energetic for gyms; warm+local for restaurants/salons; utility for pharmacies.
4. Hindi-English code-mix is natural and preferred when merchant language includes "hi". Match merchant's vibe.
5. No preambles. Get to the point immediately.
6. No fake data. If a fact isn't in the context, don't invent it.
7. Keep it short — 3-5 sentences max for merchant messages; 2-3 for customer messages.
8. Anti-patterns: "Flat X% off" (use "ServiceName @ ₹price"), multiple CTAs, promotional ALL CAPS.
9. For customer messages (send_as=merchant_on_behalf): sent FROM the merchant TO their customer.

COMPULSION LEVERS (use 1-2 per message):
- Specificity: real numbers, dates, percentages from the data
- Loss aversion: "you're missing X searches / X customers"
- Social proof: "N merchants in your area did this"
- Effort externalization: "I've drafted it — just say go"
- Curiosity: "Want to see the full breakdown?"
- Single binary: Reply YES / STOP

OUTPUT FORMAT (JSON only, no markdown):
{
  "body": "the WhatsApp message text",
  "cta": "open_ended" | "binary_yes_stop" | "none",
  "send_as": "vera" | "merchant_on_behalf",
  "suppression_key": "...",
  "rationale": "1-2 sentences explaining what lever + why now"
}"""


def build_prompt(category, merchant, trigger, customer=None):
    trigger_kind = trigger.get("kind", "unknown")
    merchant_name = merchant.get("identity", {}).get("name", "merchant")
    owner_name = merchant.get("identity", {}).get("owner_first_name", "")
    city = merchant.get("identity", {}).get("city", "")
    locality = merchant.get("identity", {}).get("locality", "")
    languages = merchant.get("identity", {}).get("languages", ["en"])
    cat_slug = category.get("slug", "")

    perf = merchant.get("performance", {})
    views = perf.get("views", 0)
    calls = perf.get("calls", 0)
    ctr = perf.get("ctr", 0)
    delta_7d = perf.get("delta_7d", {})

    peer_stats = category.get("peer_stats", {})
    peer_ctr = peer_stats.get("avg_ctr", 0)
    digest = category.get("digest", [])
    offers = merchant.get("offers", [])
    active_offers = [o for o in offers if o.get("status") == "active"]
    signals = merchant.get("signals", [])

    uses_hindi = "hi" in languages
    lang_hint = "Use natural Hindi-English code-mix." if uses_hindi else "Use English."
    payload = trigger.get("payload", {})

    kind_guidance = {
        "research_digest": "Lead with the specific research finding. Offer to draft content for the merchant.",
        "regulation_change": "Frame as urgent 'need to know'. Cite the specific rule/deadline.",
        "recall_due": "Customer recall message. Warm, personal, name the service due + available slots.",
        "perf_dip": "Show the specific number drop. Offer a concrete fix. Loss aversion.",
        "perf_spike": "Brief win celebration, then 'let's lock it in' with specific action.",
        "competitor_opened": "Matter-of-fact threat acknowledgment. Show merchant strengths. Offer defensive action.",
        "festival_upcoming": "Prep window angle. Festival + category-relevant hook. Offer to set up campaign.",
        "milestone_reached": "Quick win celebration, then momentum pivot to an action.",
        "dormant_with_vera": "Re-engage with genuinely useful info. Don't mention the dormancy.",
        "renewal_due": "Show their numbers since joining. Frame as protecting momentum.",
        "review_theme_emerged": "Show the specific pattern (N reviews, the quote). Offer to respond/address.",
        "curious_ask_due": "Ask a genuinely interesting business question — market research framing.",
        "winback_eligible": "Show missed customers + missed searches. Loss aversion.",
        "active_planning_intent": "Merchant said yes. MOVE TO ACTION immediately. Draft it for them.",
        "ipl_match_today": "Hyper-local (venue, team, time). Same-day promo draft offer.",
        "seasonal_perf_dip": "Expected seasonal — frame as 'let's get ahead of it'.",
        "customer_lapsed_soft": "Warm re-engagement from merchant. Personal, service history.",
        "appointment_tomorrow": "Day-before reminder. Warm, brief, slot details.",
        "chronic_refill_due": "Pharmacy refill reminder. Medication category (not name). Care framing.",
        "trial_followup": "Follow up trial with specific next-step offer.",
        "unverified_gbp": "Show cost of being unverified. Offer to start the fix.",
    }
    guidance = kind_guidance.get(trigger_kind, "Compose relevant, specific message using context facts.")

    cat_offers = category.get("offer_catalog", [])
    sub = merchant.get("subscription", {})

    parts = [
        f"TRIGGER KIND: {trigger_kind}",
        f"TRIGGER URGENCY: {trigger.get('urgency', 1)}/5",
        f"TRIGGER PAYLOAD: {json.dumps(payload, ensure_ascii=False)}",
        f"",
        f"MERCHANT: {merchant_name} ({cat_slug}) | {locality}, {city}",
        f"OWNER FIRST NAME: {owner_name}",
        f"SUBSCRIPTION: {sub.get('status', 'unknown')}, {sub.get('days_remaining', 0)} days remaining",
        f"PERFORMANCE (30d): views={views}, calls={calls}, CTR={ctr:.1%}",
        f"PEER MEDIAN CTR: {peer_ctr:.1%} (merchant is {'BELOW' if ctr < peer_ctr else 'ABOVE'} peer)",
        f"7d DELTA: views {delta_7d.get('views_pct', 0):+.0%}, calls {delta_7d.get('calls_pct', 0):+.0%}",
        f"ACTIVE OFFERS: {[o['title'] for o in active_offers]}",
        f"MERCHANT SIGNALS: {signals}",
        f"CUSTOMER AGGREGATE: {json.dumps(merchant.get('customer_aggregate', {}), ensure_ascii=False)}",
        f"LANGUAGE: {lang_hint}",
    ]

    if digest:
        parts.append(f"CATEGORY DIGEST: {json.dumps(digest[:3], ensure_ascii=False)}")
    seasonal = category.get("seasonal_beats", [])
    trends = category.get("trend_signals", [])
    if seasonal:
        parts.append(f"SEASONAL BEATS: {json.dumps(seasonal, ensure_ascii=False)}")
    if trends:
        parts.append(f"TREND SIGNALS: {json.dumps(trends, ensure_ascii=False)}")
    if cat_offers:
        parts.append(f"CATEGORY OFFER CATALOG: {[o['title'] for o in cat_offers[:5]]}")

    if customer:
        parts += [
            f"",
            f"CUSTOMER: {customer.get('identity', {}).get('name', 'Customer')}",
            f"CUSTOMER LANGUAGE: {customer.get('identity', {}).get('language_pref', 'en')}",
            f"STATE: {customer.get('state', 'unknown')}",
            f"LAST VISIT: {customer.get('relationship', {}).get('last_visit', 'unknown')}",
            f"SERVICES: {customer.get('relationship', {}).get('services_received', [])}",
            f"PREFERENCES: {json.dumps(customer.get('preferences', {}), ensure_ascii=False)}",
            f"CONSENT: {customer.get('consent', {}).get('scope', [])}",
            f"NOTE: send_as=merchant_on_behalf. Sound like {merchant_name} talking to their customer.",
        ]

    parts += [
        f"",
        f"TRIGGER-SPECIFIC GUIDANCE: {guidance}",
        f"SUPPRESSION KEY: {trigger.get('suppression_key', trigger_kind + ':' + merchant.get('merchant_id', '') + ':2026')}",
        f"",
        f"Return JSON only. No markdown.",
    ]
    return "\n".join(parts)


def compose(category, merchant, trigger, customer=None):
    prompt = build_prompt(category, merchant, trigger, customer)
    raw = call_claude(SYSTEM_PROMPT, prompt)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"```(?:json)?\n?", "", raw).strip().rstrip("`").strip()
    try:
        return json.loads(raw)
    except:
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            return json.loads(match.group())
        return {"body": raw[:500], "cta": "open_ended", "send_as": "vera",
                "suppression_key": "fallback", "rationale": "Fallback"}


def main():
    pairs = load_json(EXPANDED / "test_pairs.json")["pairs"]
    
    # Load all contexts
    categories = {}
    for f in (EXPANDED / "categories").glob("*.json"):
        d = load_json(f)
        categories[d["slug"]] = d

    merchants = {}
    for f in (EXPANDED / "merchants").glob("*.json"):
        d = load_json(f)
        merchants[d["merchant_id"]] = d

    customers = {}
    for f in (EXPANDED / "customers").glob("*.json"):
        d = load_json(f)
        customers[d["customer_id"]] = d

    triggers = {}
    for f in (EXPANDED / "triggers").glob("*.json"):
        d = load_json(f)
        triggers[d["id"]] = d

    results = []
    for pair in pairs:
        test_id = pair["test_id"]
        trg_id = pair["trigger_id"]
        m_id = pair["merchant_id"]
        c_id = pair.get("customer_id")

        trg = triggers.get(trg_id)
        merchant = merchants.get(m_id)
        customer = customers.get(c_id) if c_id else None

        if not trg or not merchant:
            print(f"  {test_id}: SKIP (missing trigger or merchant)")
            continue

        cat_slug = merchant.get("category_slug", "")
        category = categories.get(cat_slug)
        if not category:
            print(f"  {test_id}: SKIP (missing category {cat_slug})")
            continue

        print(f"  {test_id}: {trg_id[:50]}...", end=" ", flush=True)
        try:
            result = compose(category, merchant, trg, customer)
            row = {
                "test_id": test_id,
                "body": result.get("body", ""),
                "cta": result.get("cta", "open_ended"),
                "send_as": result.get("send_as", "vera"),
                "suppression_key": result.get("suppression_key", trg.get("suppression_key", "")),
                "rationale": result.get("rationale", "")
            }
            results.append(row)
            print(f"OK ({len(row['body'])} chars)")
        except Exception as e:
            print(f"ERROR: {e}")

        time.sleep(0.5)  # Rate limit

    out_path = Path(__file__).parent / "submission.jsonl"
    with open(out_path, "w") as f:
        for row in results:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"\nWrote {len(results)} entries to {out_path}")


if __name__ == "__main__":
    main()
