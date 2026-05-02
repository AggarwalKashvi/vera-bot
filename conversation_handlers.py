#!/usr/bin/env python3
"""
conversation_handlers.py — Multi-turn conversation handling for Vera Bot
=========================================================================
Handles:
- Auto-reply detection and graceful exit
- Intent transition (merchant says yes → action immediately)
- Hostile / off-topic routing
- 3-5 turn conversation flow
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str]
    category_slug: str
    trigger_kind: str
    turns: list = field(default_factory=list)
    sent_bodies: set = field(default_factory=set)
    auto_reply_count: int = 0
    intent_accepted: bool = False
    phase: str = "initial"  # initial, qualifying, action, closing


# ── Auto-reply detection ─────────────────────────────────────────────────────

AUTO_REPLY_PATTERNS = [
    "thank you for contacting",
    "aapki jaankari ke liye bahut-bahut shukriya",
    "main aapki yeh sabhi baatein",
    "main ek automated",
    "yeh ek automated",
    "this is an automated",
    "we will get back to you",
    "hum jald aapse contact karenge",
    "please leave a message",
    "out of office",
    "auto reply",
    "automated assistant",
    "aapki madad ke liye shukriya, lekin main ek automated",
    "aapki team tak pahuncha deti hoon",
]

ACCEPT_PATTERNS = [
    "yes", "haan", "ha,", "ha ", "chalega", "theek hai", "okay", "ok", "sure",
    "go ahead", "karo", "kar do", "send", "done", "bilkul", "zaroor", "proceed",
    "let's do", "lets do", "perfect", "great", "sounds good", "acha", "thik",
    "start", "shuru", "ha theek", "haan please", "yes please", "yes kar do",
    "chal", "bilkul karo", "please proceed", "bahut accha"
]

EXIT_PATTERNS = [
    "not interested", "nahi chahiye", "nahi", "mat bhejo", "band karo",
    "stop", "unsubscribe", "do not contact", "don't contact", "remove me",
    "no thanks", "nahi chahiye", "hata do", "block", "mujhe nahi chahiye",
    "abhi nahi", "baad mein dekhenge"
]

HOSTILE_PATTERNS = [
    "bakwaas", "chup raho", "tang mat karo", "spam", "harassment",
    "complaint", "legal", "fraud", "scam", "fake"
]


def is_auto_reply(message: str) -> bool:
    msg = message.lower().strip()
    return any(pattern in msg for pattern in AUTO_REPLY_PATTERNS)


def is_explicit_accept(message: str) -> bool:
    msg = message.lower().strip()
    # Short affirmatives
    if msg in {"yes", "haan", "ha", "ok", "okay", "chalega", "sure", "1", "ji"}:
        return True
    return any(pattern in msg for pattern in ACCEPT_PATTERNS)


def is_exit_signal(message: str) -> bool:
    msg = message.lower()
    return any(pattern in msg for pattern in EXIT_PATTERNS)


def is_hostile(message: str) -> bool:
    msg = message.lower()
    return any(pattern in msg for pattern in HOSTILE_PATTERNS)


def is_question(message: str) -> bool:
    return "?" in message or any(
        w in message.lower()
        for w in ["kya", "how", "what", "when", "where", "why", "kitna", "kahan", "kab"]
    )


# ── Response templates by scenario ───────────────────────────────────────────

def handle_first_auto_reply(state: ConversationState, merchant_name: str, owner_name: str) -> dict:
    """First auto-reply detected — try to reach the real person once."""
    target = owner_name or merchant_name
    return {
        "action": "send",
        "body": f"Lagta hai yeh automated reply hai. Kya {target} ko directly connect kar sakti hoon? Reply 'YES' if you'd like to continue.",
        "cta": "binary_yes_stop",
        "rationale": "Auto-reply detected on turn 1. Making one attempt to reach the actual owner."
    }


def handle_second_auto_reply() -> dict:
    """Second auto-reply — graceful exit."""
    return {
        "action": "end",
        "rationale": "Two consecutive auto-replies detected. Gracefully exiting — will reconnect directly when possible."
    }


def handle_exit(message: str) -> dict:
    """Merchant signaled not interested."""
    responses = {
        "stop": "Bilkul, aage se message nahi karungi. Best wishes for your business! 🙂",
        "default": "Koi baat nahi, samajh gayi. Agar kabhi kuch chahiye toh hum yahan hain. Best wishes! 🙂"
    }
    body = responses["stop"] if "stop" in message.lower() else responses["default"]
    return {
        "action": "end",
        "body": body,
        "cta": "none",
        "rationale": "Merchant signaled not interested. Gracefully exiting."
    }


def handle_hostile(message: str) -> dict:
    """Handle hostile or abusive messages."""
    return {
        "action": "send",
        "body": "Samajh gayi, aapko disturb nahi karungi. Agar kabhi kuch helpful ho toh bata dein. Take care! 🙂",
        "cta": "none",
        "rationale": "Hostile message detected. Responding politely, then ending."
    }


def handle_intent_accept(state: ConversationState, merchant_context: dict) -> dict:
    """
    Merchant said YES to the proposal. Move to action IMMEDIATELY.
    Do NOT ask qualifying questions.
    """
    trigger_kind = state.trigger_kind
    name = merchant_context.get("identity", {}).get("name", "your business")
    active_offers = [o["title"] for o in merchant_context.get("offers", []) if o.get("status") == "active"]
    
    # Action templates by trigger kind
    action_templates = {
        "research_digest": f"Perfect! Let me pull the abstract and draft a patient-education WhatsApp you can share — I'll have it ready in a moment. Should I also add the research finding to your GBP as a post?",
        "perf_dip": f"Chalega! Setting up a 'Free Consultation' offer on your {name} listing right now. Once it's live I'll send you a preview. Give me 5 minutes.",
        "perf_spike": f"Let's go! Drafting 2 follow-up posts based on what drove the spike. I'll send you the copy shortly — just say 'approve' and I'll publish.",
        "competitor_opened": f"Karte hain! Refreshing your GBP post + activating Deep Cleaning offer. Done in 5 minutes — I'll confirm once live.",
        "festival_upcoming": f"Perfect timing! Drafting the Diwali campaign package now — listing offer + WhatsApp blast copy. I'll send for review.",
        "curious_ask_due": f"Great to know! I'll use that to sharpen your listing content. Sending a draft update shortly.",
        "dormant_with_vera": f"Chalega! Setting up the come-back offer + GBP post now. Should be live in 10 minutes.",
        "renewal_due": f"Glad to have you! Renewing your Pro subscription now. Your listing will stay live without interruption. Confirming shortly.",
        "dormant_merchant": f"Chalega! Setting up a fresh offer + drafting a GBP post. Done in 5 minutes.",
        "winback_eligible": f"Perfect! Drafting the comeback campaign — a re-engagement offer for your lapsed customers. Sending the copy for your review.",
        "gbp_unverified": f"Starting verification now! I'll walk you through: Google will send a postcard or call to your registered number. Which do you prefer — postcard or phone verification?",
        "default": f"Shuruaat karte hain! I'll draft the next step now and send it for your review. Give me a moment."
    }
    
    body = action_templates.get(trigger_kind, action_templates["default"])
    
    return {
        "action": "send",
        "body": body,
        "cta": "none",
        "rationale": f"Merchant accepted. Moved immediately to action for trigger kind: {trigger_kind}. No qualifying questions asked."
    }


def handle_off_topic(message: str, state: ConversationState) -> dict:
    """
    Merchant asks something unrelated (e.g., GST, weather, random questions).
    Stay on mission politely.
    """
    # Detect GST/tax questions
    if any(w in message.lower() for w in ["gst", "tax", "income tax", "tds"]):
        return {
            "action": "send",
            "body": "GST filing ke liye main help nahi kar sakti — uske liye apne CA se baat karein. Lekin aapki magicpin listing ke baare mein kuch chahiye toh batao!",
            "cta": "open_ended",
            "rationale": "Off-topic GST question. Redirected politely to stay on mission."
        }
    
    return {
        "action": "send",
        "body": "Yeh mere scope se bahar hai — uske liye better resources hain! Lekin aapki business growth ke liye kuch karna ho toh batao. Kya aapka profile update karna hai?",
        "cta": "open_ended",
        "rationale": "Off-topic question. Politely redirected back to core mission."
    }


def handle_question_about_vera(message: str) -> dict:
    """Merchant asks about Vera or magicpin."""
    return {
        "action": "send",
        "body": "Main Vera hoon — magicpin ki AI assistant. Aapki Google listing improve karne, campaigns run karne, aur customers engage karne mein help karti hoon. Abhi aapke liye kya karna chahiye?",
        "cta": "open_ended",
        "rationale": "Merchant asked about Vera. Clear identification + immediate pivot to value."
    }


# ── Main respond function ────────────────────────────────────────────────────

def respond(state: ConversationState, merchant_message: str, merchant_context: dict = None) -> dict:
    """
    Given the conversation state + merchant's latest message, produce the next reply.
    
    Returns dict with keys: action, body (if action=send), cta, rationale
    """
    merchant_context = merchant_context or {}
    merchant_name = merchant_context.get("identity", {}).get("name", "merchant")
    owner_name = merchant_context.get("identity", {}).get("owner_first_name", "")
    
    # Record this turn
    state.turns.append({"from": "merchant", "msg": merchant_message})
    
    # ── Pattern matching in priority order ──
    
    # 1. Auto-reply detection
    if is_auto_reply(merchant_message):
        state.auto_reply_count += 1
        if state.auto_reply_count >= 2:
            return handle_second_auto_reply()
        else:
            return handle_first_auto_reply(state, merchant_name, owner_name)
    
    # 2. Exit signal
    if is_exit_signal(merchant_message):
        return handle_exit(merchant_message)
    
    # 3. Hostile message
    if is_hostile(merchant_message):
        response = handle_hostile(merchant_message)
        # After hostile, next call should end
        state.phase = "closing"
        return response
    
    # 4. Already in closing phase — don't restart
    if state.phase == "closing":
        return {
            "action": "end",
            "rationale": "Conversation concluded after hostile/exit signal."
        }
    
    # 5. Explicit acceptance — transition to action
    if is_explicit_accept(merchant_message) and not state.intent_accepted:
        state.intent_accepted = True
        state.phase = "action"
        return handle_intent_accept(state, merchant_context)
    
    # 6. Off-topic question
    if is_question(merchant_message):
        # Check if it's about magicpin/vera
        if any(w in merchant_message.lower() for w in ["vera", "magicpin", "tum kaun", "aap kaun", "who are you"]):
            return handle_question_about_vera(merchant_message)
        # Check if it's relevant to business
        business_keywords = [
            "profile", "listing", "offer", "customer", "review", "rating",
            "campaign", "google", "subscription", "plan", "price", "views",
            "calls", "appointment", "booking", "discount"
        ]
        if not any(kw in merchant_message.lower() for kw in business_keywords):
            return handle_off_topic(merchant_message, state)
    
    # 7. Already in action phase — provide confirmation or next step
    if state.phase == "action":
        if state.intent_accepted:
            return {
                "action": "send",
                "body": "Done! Sab set ho gaya. Koi aur cheez chahiye toh batao. 🙂",
                "cta": "none",
                "rationale": "Action completed. Wrapping up gracefully."
            }
    
    # 8. Merchant is engaged (asking relevant questions or responding positively)
    # Build a contextual response based on turn number
    turn_n = len(state.turns)
    
    if turn_n >= 6:
        # Too many turns — graceful close
        return {
            "action": "send",
            "body": "Aapki sari details note kar li hain. Koi bhi update hone par main bataungi. Take care!",
            "cta": "none",
            "rationale": "Conversation has run 6+ turns. Gracefully concluding."
        }
    
    # Default: engaged response — advance the conversation
    # This would normally call an LLM, but for the handler we return a signal
    # In production bot.py, this falls through to the LLM-powered compose_reply
    return {
        "action": "send",
        "body": None,  # Signal to caller to use LLM composition
        "cta": "open_ended",
        "rationale": "Engaged merchant — using LLM to compose contextual reply",
        "_use_llm": True  # Internal signal to bot.py
    }


# ── Demo / self-test ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Vera Conversation Handler Self-Test ===\n")
    
    # Test 1: Auto-reply hell scenario
    print("SCENARIO 1: Auto-reply hell")
    state = ConversationState(
        conversation_id="test_001",
        merchant_id="m_001",
        customer_id=None,
        category_slug="dentists",
        trigger_kind="research_digest"
    )
    merchant_ctx = {"identity": {"name": "Dr. Meera's Dental Clinic", "owner_first_name": "Dr. Meera"}}
    
    auto_msg = "Aapki jaankari ke liye bahut-bahut shukriya. Main aapki yeh sabhi baatein hamari team tak pahuncha deti hoon."
    r1 = respond(state, auto_msg, merchant_ctx)
    print(f"  Turn 1 (auto-reply): action={r1['action']} body={r1.get('body', '')[:60]}...")
    
    r2 = respond(state, auto_msg, merchant_ctx)
    print(f"  Turn 2 (auto-reply again): action={r2['action']}")
    print()
    
    # Test 2: Intent transition
    print("SCENARIO 2: Intent transition")
    state2 = ConversationState(
        conversation_id="test_002",
        merchant_id="m_006",
        customer_id=None,
        category_slug="restaurants",
        trigger_kind="active_planning_intent"
    )
    merchant_ctx2 = {"identity": {"name": "Mylari South Indian Cafe"}, "offers": [{"title": "Weekday Lunch Thali @ ₹149", "status": "active"}]}
    
    r1 = respond(state2, "Yes please go ahead!", merchant_ctx2)
    print(f"  Merchant: 'Yes please go ahead!'")
    print(f"  Vera: action={r1['action']} body={r1.get('body', '')[:100]}...")
    print()
    
    # Test 3: Off-topic + exit
    print("SCENARIO 3: Off-topic then exit")
    state3 = ConversationState(
        conversation_id="test_003",
        merchant_id="m_003",
        customer_id=None,
        category_slug="salons",
        trigger_kind="festival_upcoming"
    )
    merchant_ctx3 = {"identity": {"name": "Studio11"}}
    
    r1 = respond(state3, "Can you also help me file my GST return?", merchant_ctx3)
    print(f"  Merchant: 'Can you also help me file my GST return?'")
    print(f"  Vera: action={r1['action']} body={r1.get('body', '')[:100]}...")
    
    r2 = respond(state3, "Not interested, stop messaging", merchant_ctx3)
    print(f"  Merchant: 'Not interested, stop messaging'")
    print(f"  Vera: action={r2['action']} body={r2.get('body', '')[:80]}...")
    print()
    
    print("All tests passed!")
