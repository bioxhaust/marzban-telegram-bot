"""FreeKassa SCI (pay.fk.money) — подписи по схеме из официальной документации / fk.money."""

from __future__ import annotations

import hashlib
from typing import Mapping
from urllib.parse import urlencode

PAYMENT_URL = "https://pay.fk.money/"

# IP серверов оповещений (из актуальной документации FK; при необходимости обнови).
FREEKASSA_NOTIFY_IPS = frozenset(
    {
        "168.119.157.136",
        "168.119.60.227",
        "178.154.197.79",
        "51.250.54.238",
    }
)


def sci_signature(
    merchant_id: int,
    amount: float,
    secret_word_1: str,
    currency: str,
    order_id: str,
) -> str:
    s = f"{merchant_id}:{amount}:{secret_word_1}:{currency}:{order_id}"
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def build_payment_url(
    *,
    merchant_id: int,
    amount: float,
    currency: str,
    order_id: str,
    secret_word_1: str,
    lang: str = "ru",
) -> str:
    sig = sci_signature(merchant_id, amount, secret_word_1, currency, order_id)
    params: dict[str, str | int | float] = {
        "m": merchant_id,
        "oa": amount,
        "currency": currency,
        "o": order_id,
        "s": sig,
        "lang": lang,
    }
    q = urlencode(params, safe="")
    return f"{PAYMENT_URL}?{q}"


def notification_signature(
    merchant_id: int,
    amount_raw: str,
    secret_word_2: str,
    merchant_order_id: str,
) -> str:
    s = f"{merchant_id}:{amount_raw}:{secret_word_2}:{merchant_order_id}"
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def verify_notification(
    data: Mapping[str, str],
    *,
    secret_word_2: str,
) -> bool:
    try:
        mid = int(data.get("MERCHANT_ID") or 0)
        amount_raw = str(data.get("AMOUNT") or "").strip().replace(",", ".")
        oid = data.get("MERCHANT_ORDER_ID") or ""
        sign = (data.get("SIGN") or "").lower()
    except (TypeError, ValueError):
        return False
    if not oid or not sign or not amount_raw:
        return False
    exp = notification_signature(mid, amount_raw, secret_word_2, oid)
    return exp.lower() == sign
