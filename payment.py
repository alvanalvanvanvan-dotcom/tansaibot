"""Telegram Stars payment integration for tansaibot (#47).

Enables users to upgrade to Premium tier by paying with Telegram Stars.

Features:
  - /premium command: show pricing and "Buy with Stars" button
  - Stars payment flow via Telegram Payments API
  - Automatic tier upgrade on successful payment
  - /paysupport for payment support
  
Env vars:
    STARS_PRICE_PREMIUM   — Stars price for Premium upgrade (default: 100)
    STARS_PRICE_MONTHLY   — Stars price for monthly premium (default: 50)

Usage:
    from payment import PremiumProduct, build_premium_invoice
    invoice = build_premium_invoice(user_id)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

STARS_PREMIUM_ONCE = int(os.getenv("STARS_PRICE_PREMIUM", "100"))   # Telegram Stars
STARS_PREMIUM_MONTHLY = int(os.getenv("STARS_PRICE_MONTHLY", "50"))


@dataclass
class PremiumProduct:
    title: str
    description: str
    payload: str       # Internal identifier
    stars_amount: int  # In XTR (Telegram Stars)


PRODUCTS: dict[str, PremiumProduct] = {
    "premium_once": PremiumProduct(
        title="⭐ Premium Selamanya",
        description="Akses unlimited semua model AI, prioritas support, fitur eksklusif.",
        payload="premium_once",
        stars_amount=STARS_PREMIUM_ONCE,
    ),
    "premium_monthly": PremiumProduct(
        title="⭐ Premium 30 Hari",
        description="Akses Premium selama 30 hari. Perpanjang kapan saja.",
        payload="premium_monthly",
        stars_amount=STARS_PREMIUM_MONTHLY,
    ),
}


async def send_premium_invoice(
    update: "Update",
    context: "ContextTypes.DEFAULT_TYPE",
    product_key: str = "premium_monthly",
) -> None:
    """Send a Telegram Stars invoice for premium upgrade."""
    assert update.message is not None
    product = PRODUCTS.get(product_key)
    if product is None:
        await update.message.reply_text("❌ Produk tidak ditemukan.")
        return

    try:
        await context.bot.send_invoice(
            chat_id=update.effective_chat.id,
            title=product.title,
            description=product.description,
            payload=product.payload,
            provider_token="",  # Empty for Stars (XTR)
            currency="XTR",     # Telegram Stars
            prices=[{"label": product.title, "amount": product.stars_amount}],
        )
    except Exception as exc:
        logger.error("Failed to send Stars invoice: %s", exc)
        await update.message.reply_text(
            f"❌ Gagal membuat invoice: {exc}\n"
            "Pastikan bot sudah diaktifkan untuk Telegram Stars payment."
        )


async def handle_pre_checkout(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    """Answer pre-checkout query (required by Telegram Payments API)."""
    query = update.pre_checkout_query
    if query is None:
        return
    # Always approve unless we need to validate stock etc.
    await query.answer(ok=True)


async def handle_successful_payment(
    update: "Update",
    context: "ContextTypes.DEFAULT_TYPE",
) -> None:
    """Handle successful Stars payment — upgrade user tier."""
    assert update.message is not None
    assert update.effective_user is not None

    payment = update.message.successful_payment
    if payment is None:
        return

    user_id = update.effective_user.id
    payload = payment.invoice_payload
    stars = payment.total_amount

    logger.info("Stars payment: user=%d payload=%s stars=%d", user_id, payload, stars)

    # Upgrade user tier
    db_path: str = context.application.bot_data.get("db_path", "")
    if db_path:
        import db
        await db.upsert_user_prefs(db_path, user_id, tier="premium")
        await db.log_audit(
            db_path,
            admin_id=0,
            action="payment_stars",
            target_id=user_id,
            detail=f"payload={payload} stars={stars}",
        )

    product_name = PRODUCTS.get(payload, PremiumProduct("Premium", "", "", 0)).title
    await update.message.reply_text(
        f"🎉 <b>Pembayaran Berhasil!</b>\n\n"
        f"✅ {product_name}\n"
        f"⭐ {stars} Stars\n\n"
        f"Akun kamu sudah diupgrade ke <b>Premium</b>! 🚀\n"
        f"Gunakan /insights untuk melihat benefit premiummu.",
        parse_mode="HTML",
    )


def premium_keyboard():
    """Build inline keyboard for premium upgrade options."""
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        buttons = [
            [InlineKeyboardButton(
                f"⭐ {STARS_PREMIUM_MONTHLY} Stars — Premium 30 Hari",
                callback_data="buy_premium_monthly",
            )],
            [InlineKeyboardButton(
                f"⭐ {STARS_PREMIUM_ONCE} Stars — Premium Selamanya",
                callback_data="buy_premium_once",
            )],
        ]
        return InlineKeyboardMarkup(buttons)
    except ImportError:
        return None
