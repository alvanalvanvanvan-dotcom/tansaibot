"""Cost tracking and quota management (#44 #45 #46).

Tracks token usage per user and optionally per model pricing.
Supports user tiers: free / premium / admin.

Model pricing (USD per 1k tokens) — update as needed.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model pricing (USD per 1k tokens) — input, output
# ---------------------------------------------------------------------------
# Format: "model_id": (price_per_1k_input, price_per_1k_output)
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o":            (0.0025,  0.010),
    "gpt-4o-mini":       (0.00015, 0.0006),
    "gpt-4-turbo":       (0.010,   0.030),
    "gpt-3.5-turbo":     (0.0005,  0.0015),
    "claude-3-5-sonnet": (0.003,   0.015),
    "claude-3-haiku":    (0.00025, 0.00125),
    "gemini-1.5-pro":    (0.00125, 0.005),
    "gemini-1.5-flash":  (0.000075,0.0003),
    # Fallback for unknown models
    "_default":          (0.001,   0.002),
}

IDR_PER_USD = 16_000  # rough estimate — update as needed

# ---------------------------------------------------------------------------
# Tier definitions
# ---------------------------------------------------------------------------
TIER_FREE = "free"
TIER_PREMIUM = "premium"
TIER_ADMIN = "admin"

TIER_DAILY_TOKEN_LIMIT: dict[str, int] = {
    TIER_FREE:    100_000,   # ~100k tokens/day
    TIER_PREMIUM: 1_000_000, # ~1M tokens/day
    TIER_ADMIN:   0,         # unlimited
}

TIER_ALLOWED_MODELS: dict[str, list[str]] = {
    TIER_FREE:    ["gpt-4o-mini", "gpt-3.5-turbo", "gemini-1.5-flash", "claude-3-haiku"],
    TIER_PREMIUM: [],  # empty = allow all
    TIER_ADMIN:   [],  # empty = allow all
}


# ---------------------------------------------------------------------------
# Cost calculation helpers
# ---------------------------------------------------------------------------

def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """Calculate cost in USD for a given model and token count."""
    price_in, price_out = MODEL_PRICING.get(model, MODEL_PRICING["_default"])
    return (tokens_in / 1000 * price_in) + (tokens_out / 1000 * price_out)


def cost_idr(model: str, tokens_in: int, tokens_out: int) -> float:
    return cost_usd(model, tokens_in, tokens_out) * IDR_PER_USD


def format_cost(usd: float) -> str:
    """Format cost as 'Rp X.XXX (US$ 0.0001)'."""
    idr = usd * IDR_PER_USD
    if idr < 1:
        idr_str = f"Rp {idr:.4f}"
    elif idr < 1000:
        idr_str = f"Rp {idr:.2f}"
    else:
        idr_str = f"Rp {idr:,.0f}"
    return f"{idr_str} (US${usd:.6f})"


def is_model_allowed(model: str, tier: str) -> bool:
    """Check if a model is accessible for the given tier."""
    allowed = TIER_ALLOWED_MODELS.get(tier, [])
    if not allowed:  # empty list = allow all
        return True
    return any(model.startswith(m) or m in model for m in allowed)


def get_fallback_model(tier: str, current_model: str) -> str | None:
    """Return a cheaper fallback model for the tier, or None if current is ok."""
    allowed = TIER_ALLOWED_MODELS.get(tier, [])
    if not allowed:
        return None  # unrestricted
    if is_model_allowed(current_model, tier):
        return None  # already ok
    return allowed[0] if allowed else None  # first allowed model


def get_user_tier(status: str, is_admin: bool) -> str:
    """Derive user tier from DB status and admin flag."""
    if is_admin:
        return TIER_ADMIN
    if status == "premium":
        return TIER_PREMIUM
    return TIER_FREE
