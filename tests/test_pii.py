import pytest

from sysdesign.pii import contains_pii, luhn_ok, redact, redact_obj


@pytest.mark.parametrize("text,token", [
    ("card 4111 1111 1111 1111 please", "[CARD]"),
    ("card 4111-1111-1111-1111", "[CARD]"),
    ("amex 378282246310005", "[CARD]"),
    ("mail me at ava.k+orders@example.com", "[EMAIL]"),
    ("ssn 123-45-6789", "[SSN]"),
    ("call (555) 123-4567", "[PHONE]"),
    ("call +1 555.123.4567", "[PHONE]"),
])
def test_redacts_each_pii_kind(text, token):
    out = redact(text)
    assert token in out
    assert not any(ch.isdigit() for ch in out.split(token)[0][-4:]) or token == "[EMAIL]"


def test_non_luhn_long_number_is_kept():
    # order/tracking numbers must not be mangled
    assert redact("tracking 1234567890123") == "tracking 1234567890123"
    assert not luhn_ok("1234567890123")


def test_order_ids_untouched():
    assert redact("where is KG-100001") == "where is KG-100001"


def test_card_number_inside_error_message_is_redacted():
    msg = "payment failed for card 5555555555554444: declined"
    assert "5555555555554444" not in redact(msg)


def test_redact_obj_recurses():
    out = redact_obj({"a": ["x@y.io", {"b": "123-45-6789"}], "n": 3})
    assert out == {"a": ["[EMAIL]", {"b": "[SSN]"}], "n": 3}


def test_contains_pii():
    assert contains_pii("my email is a@b.co")
    assert not contains_pii("what is the return window")
