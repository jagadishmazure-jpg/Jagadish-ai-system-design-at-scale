"""Structured model output: one schema, strict validation, retry once, then fall back.

Order: small model -> (invalid) retry small once -> (invalid) large model -> (invalid) give up and
ask the user. Repair and fallback counts are returned so they can be logged as rates.
"""
from __future__ import annotations

import json

from pydantic import ValidationError

from .llm import ProviderError
from .tools import Strict


class RefundDraft(Strict):
    order_id: str | None = None
    amount: float | None = None
    reason: str = "other"


async def extract(providers: dict, prompt: str, model=RefundDraft, chain=("small", "small", "large")):
    fields = {k: "any" for k in model.model_fields}
    attempts, usage_total, errors = [], [], []
    for tier in chain:
        try:
            raw, usage = await providers[tier].extract(prompt, fields)
        except ProviderError as e:
            errors.append(f"{tier}:{type(e).__name__}")
            attempts.append(tier)
            continue
        usage_total.append((tier, usage))
        attempts.append(tier)
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            return model.model_validate(data), {"attempts": attempts, "usage": usage_total, "invalid": len(
                attempts) - 1, "errors": errors}
        except (ValueError, ValidationError):
            errors.append(f"{tier}:invalid_schema")
    return None, {"attempts": attempts, "usage": usage_total, "invalid": len(attempts), "errors": errors}
