"""Синхронная выдача подписки + текст итогового сообщения (бот и HTTP-вебхук)."""

from __future__ import annotations

import logging
import os
from typing import Optional

from database import Database, User
from marzban_client import MarzbanClient

logger = logging.getLogger(__name__)

_MONTH_NAMES_RU = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def format_access_until(iso_dt: str) -> str:
    from datetime import datetime, timezone

    s = iso_dt.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"{dt.day} {_MONTH_NAMES_RU[dt.month - 1]} {dt.year}"


def format_telegram_blocks(*blocks: str) -> str:
    return "\n\n".join(b.strip() for b in blocks if b and b.strip())


def _invite_slots_for_months(months: int) -> int:
    try:
        per = max(1, int(os.getenv("INVITES_PER_SUB_MONTH", "3").strip()))
    except ValueError:
        per = 3
    return per * max(1, int(months))


def deliver_subscription_body(
    db: Database,
    marzban_client: Optional[MarzbanClient],
    admin_telegram_id: int,
    user: User,
    months: int,
    plan_prefix: str,
    paid_title_line: str,
) -> str:
    days = months * 30
    expires_at = db.grant_subscription(
        user.id, plan_name=f"{plan_prefix}_{days}d", days=days
    )
    invite_slots = _invite_slots_for_months(months)
    db.add_invite_balance(user.id, invite_slots)

    marzban_text = (
        "Техничка: Marzban не подключён (.env). Заведение без турникетов."
    )
    if marzban_client:
        try:
            marzban_result = marzban_client.create_or_update_user(
                marzban_username=user.marzban_username or None,
                telegram_id=user.telegram_id,
                telegram_username=user.username or None,
                days=days,
            )
            db.set_user_marzban_profile(
                user.id,
                marzban_username=marzban_result["username"],
                marzban_subscription_url=marzban_result["subscription_url"],
            )
            marzban_text = "Турникет выдал доступ."
            if marzban_result["subscription_url"]:
                marzban_text += (
                    "\n\nСсылка на подписку (вставьте в приложение):\n\n"
                    f"{marzban_result['subscription_url']}"
                )
        except Exception as exc:
            logger.exception("Marzban sync failed")
            marzban_text = (
                f"Абонемент в базе есть, турникет Marzban глючит: {exc}"
            )

    if user.can_generate_invites == 0:
        db.update_user_invite_policy(
            user.id, can_generate_invites=1, invite_generation_limit=0
        )

    refreshed = db.get_user_by_telegram_id(user.telegram_id)
    ibal = refreshed.invite_balance if refreshed else invite_slots
    invite_block = "\n".join(
        [
            f"+{invite_slots} приглашений в карман за эту оплату.",
            f"Всего инвайтов в кармане: {ibal}",
        ]
    )
    return format_telegram_blocks(
        paid_title_line,
        f"Доступ до: {format_access_until(expires_at)}",
        invite_block,
        marzban_text.strip(),
        "Можешь звать своих — «Пустить +1».",
    )
