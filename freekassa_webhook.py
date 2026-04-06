"""
Приём оповещений FreeKassa (URL оповещения в личном кабинете).

Нужен публичный HTTPS. Пример запуска:
  pip install flask
  export FLASK_APP=freekassa_webhook.py
  flask run -h 0.0.0.0 -p 8080

В проде: nginx → proxy_pass на этот порт + сертификат Let's Encrypt.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests
from dotenv import load_dotenv
from flask import Flask, request

from database import Database
from freekassa import FREEKASSA_NOTIFY_IPS, verify_notification
from marzban_client import MarzbanClient
from subscription_fulfillment import deliver_subscription_body

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "./vpnbot.db")
FREEKASSA_SECRET_WORD_2 = os.getenv("FREEKASSA_SECRET_WORD_2", "").strip()
try:
    FREEKASSA_MERCHANT_ID = int(
        os.getenv("FREEKASSA_MERCHANT_ID", "0").strip() or "0"
    )
except ValueError:
    FREEKASSA_MERCHANT_ID = 0
SKIP_IP = os.getenv("FREEKASSA_SKIP_IP_VERIFY", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

MARZBAN_BASE_URL = os.getenv("MARZBAN_BASE_URL", "").strip()
MARZBAN_ADMIN_USERNAME = os.getenv("MARZBAN_ADMIN_USERNAME", "").strip()
MARZBAN_ADMIN_PASSWORD = os.getenv("MARZBAN_ADMIN_PASSWORD", "").strip()
MARZBAN_SUBSCRIPTION_URL_PREFIX = os.getenv(
    "MARZBAN_SUBSCRIPTION_URL_PREFIX", ""
).strip()
MARZBAN_INBOUND_TAGS = [
    tag.strip()
    for tag in os.getenv("MARZBAN_INBOUND_TAGS", "").split(",")
    if tag.strip()
]

db = Database(DB_PATH)
marzban_client: Optional[MarzbanClient] = None
if MARZBAN_BASE_URL and MARZBAN_ADMIN_USERNAME and MARZBAN_ADMIN_PASSWORD:
    marzban_client = MarzbanClient(
        base_url=MARZBAN_BASE_URL,
        username=MARZBAN_ADMIN_USERNAME,
        password=MARZBAN_ADMIN_PASSWORD,
        subscription_url_prefix=MARZBAN_SUBSCRIPTION_URL_PREFIX,
        inbound_tags=MARZBAN_INBOUND_TAGS,
    )

app = Flask(__name__)


def _client_ip() -> str:
    xri = request.headers.get("X-Real-IP")
    if xri:
        return xri.strip()
    if request.access_route:
        return request.access_route[0]
    return request.remote_addr or ""


def _telegram_send(chat_id: int, text: str) -> None:
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN пуст — сообщение не отправлено")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=45,
    )
    if r.status_code != 200:
        logger.error("Telegram sendMessage: %s %s", r.status_code, r.text[:500])


@app.route("/freekassa/notify", methods=["GET", "POST"])
def freekassa_notify():
    if not FREEKASSA_SECRET_WORD_2 or not FREEKASSA_MERCHANT_ID:
        logger.error("FreeKassa secrets не заданы")
        return "NO", 500

    data = {k: str(v) for k, v in request.values.items()}

    ip = _client_ip()
    if not SKIP_IP and ip not in FREEKASSA_NOTIFY_IPS:
        logger.warning("Запрос не с IP FreeKassa: %s", ip)
        return "NO", 403

    try:
        mid = int(data.get("MERCHANT_ID", "0"))
        amount = float(str(data.get("AMOUNT", "0")).replace(",", "."))
    except (TypeError, ValueError):
        return "NO", 400

    if mid != FREEKASSA_MERCHANT_ID:
        return "NO", 400

    if not verify_notification(data, secret_word_2=FREEKASSA_SECRET_WORD_2):
        logger.warning("Неверная подпись FreeKassa")
        return "NO", 400

    order_id = data.get("MERCHANT_ORDER_ID", "")
    if not order_id:
        return "NO", 400

    row = db.try_complete_freekassa_order(order_id, amount)
    if row is None:
        # Уже оплачен или нет заказа — отвечаем YES, чтобы не репостили вечно
        return "YES", 200

    user = db.get_user_by_id(int(row["user_id"]))
    if user is None:
        logger.error("Пользователь user_id=%s не найден", row["user_id"])
        return "YES", 200

    months = int(row["months"])
    try:
        body = deliver_subscription_body(
            db,
            marzban_client,
            ADMIN_TELEGRAM_ID,
            user,
            months,
            "freekassa",
            f"🎟 Вход на {months} мес. оплачен (FreeKassa).",
        )
    except Exception:
        logger.exception("Ошибка выдачи подписки после FreeKassa")
        return "NO", 500

    promo_raw = row["promo_code_id"] if "promo_code_id" in row.keys() else None
    promo_id = int(promo_raw) if promo_raw is not None else None
    db.record_promo_use_on_payment_success(user.id, promo_id)
    db.credit_referrer_from_buyer_payment(user.id, float(amount))

    _telegram_send(user.telegram_id, body)
    return "YES", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("FREEKASSA_WEBHOOK_PORT", "8080")))
