import pytest

from sysdesign.router import classify, route


@pytest.mark.parametrize("text,intent,rung,tier", [
    ("hi", "greeting", "rules", None),
    ("Thanks!", "thanks", "rules", None),
    ("where is my order KG-100001", "order_status", "rules", None),
    ("what is the return window for electronics", "policy", "small_model", "small"),
    ("I want a refund for KG-100002", "refund", "classifier", "small"),
    ("price of SKU-KETTLE", "price", "classifier", None),
    ("my split shipment from two orders has a missing item", "complex", "large_model", "large"),
])
def test_ladder(text, intent, rung, tier):
    r = route(text)
    assert (r.intent, r.rung, r.model_tier) == (intent, rung, tier)


@pytest.mark.parametrize("text", [
    "I want to cancel my membership", "cancel my subscription now", "this is a billing dispute",
    "I want to dispute this charge", "thanks, but I'm filing a chargeback", "my lawyer will call",
    "hi, I want to speak to a human",
])
def test_hitl_intents_never_reach_ai(text):
    r = route(text)
    assert r.intent == "escalate" and r.model_tier is None and r.rung == "human"


def test_hitl_wins_even_with_ladder_off():
    assert route("billing dispute", ladder_enabled=False).intent == "escalate"


def test_ladder_off_sends_everything_to_large():
    assert route("hi", ladder_enabled=False).model_tier == "large"


def test_refund_with_order_id_not_treated_as_status():
    assert route("where is the refund for KG-100002").intent == "refund"


def test_classify_unknown():
    assert classify("zxqv") == ("unknown", 0.0)
