import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from dotenv import load_dotenv
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from cryptopay_client import CryptoPayClient, CryptoPayError
from database import Database, User
from marzban_client import MarzbanClient
from subscription_fulfillment import (
    deliver_subscription_body,
    format_access_until,
    format_telegram_blocks,
)


logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
SUPPORT_TELEGRAM_USERNAME = (
    os.getenv("SUPPORT_TELEGRAM_USERNAME", "pewie").strip().lstrip("@")
)
try:
    _lb = int(os.getenv("ADMIN_WIPE_LOOKBACK", "400").strip() or "400")
except ValueError:
    _lb = 400
ADMIN_WIPE_LOOKBACK = max(50, min(2000, _lb))
try:
    _fs = int(os.getenv("ADMIN_WIPE_FAIL_STREAK", "22").strip() or "22")
except ValueError:
    _fs = 22
ADMIN_WIPE_FAIL_STREAK_EXIT = max(5, min(200, _fs))
DB_PATH = os.getenv("DB_PATH", "./vpnbot.db")
MARZBAN_BASE_URL = os.getenv("MARZBAN_BASE_URL", "").strip()
MARZBAN_ADMIN_USERNAME = os.getenv("MARZBAN_ADMIN_USERNAME", "").strip()
MARZBAN_ADMIN_PASSWORD = os.getenv("MARZBAN_ADMIN_PASSWORD", "").strip()
MARZBAN_SUBSCRIPTION_URL_PREFIX = os.getenv("MARZBAN_SUBSCRIPTION_URL_PREFIX", "").strip()
MARZBAN_INBOUND_TAGS = [
    tag.strip()
    for tag in os.getenv("MARZBAN_INBOUND_TAGS", "").split(",")
    if tag.strip()
]


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)).strip())
    except ValueError:
        return default


CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN", "").strip()
CRYPTO_PAY_TESTNET = os.getenv("CRYPTO_PAY_TESTNET", "").strip().lower() in (
    "1",
    "true",
    "yes",
)
CRYPTO_FIAT = (os.getenv("CRYPTO_FIAT", "USD").strip().upper() or "USD")
# Базовая цена за 1 месяц в CRYPTO_FIAT; скидки на пакеты 3 / 6 / 12 мес. (доля 0–1)
CRYPTO_PRICE_PER_MONTH = _env_float("CRYPTO_PRICE_PER_MONTH", 2.0)


def _env_discount_fraction(key: str, default_percent: float) -> float:
    try:
        p = float(os.getenv(key, str(default_percent)).strip())
        return max(0.0, min(0.95, p / 100.0))
    except ValueError:
        return max(0.0, min(0.95, default_percent / 100.0))


CRYPTO_DISCOUNT_3M = _env_discount_fraction("CRYPTO_DISCOUNT_3M_PCT", 5.0)
CRYPTO_DISCOUNT_6M = _env_discount_fraction("CRYPTO_DISCOUNT_6M_PCT", 10.0)
CRYPTO_DISCOUNT_12M = _env_discount_fraction("CRYPTO_DISCOUNT_12M_PCT", 20.0)
_CRYPTO_PACK_DISCOUNT = {
    3: CRYPTO_DISCOUNT_3M,
    6: CRYPTO_DISCOUNT_6M,
    12: CRYPTO_DISCOUNT_12M,
}
try:
    CRYPTO_POLL_INTERVAL = float(
        os.getenv("CRYPTO_POLL_INTERVAL", "15").strip() or "15"
    )
except ValueError:
    CRYPTO_POLL_INTERVAL = 15.0
CRYPTO_POLL_INTERVAL = max(5.0, min(120.0, CRYPTO_POLL_INTERVAL))

# Валюта отображения тарифа и база ₽/мес (имя FREEKASSA_* историческое; касса не используется).
FREEKASSA_CURRENCY = (
    os.getenv("FREEKASSA_CURRENCY", "RUB").strip().upper() or "RUB"
)
FREEKASSA_PRICE_PER_MONTH_RUB = _env_float("FREEKASSA_PRICE_PER_MONTH_RUB", 200.0)
_pp_raw = os.getenv("PAYMENT_PROVIDER", "auto").strip().lower()
if _pp_raw == "freekassa":
    _pp_raw = "auto"
PAYMENT_PROVIDER = _pp_raw if _pp_raw in ("auto", "crypto") else "auto"
TELEGRAM_STARS_ENABLED = os.getenv("TELEGRAM_STARS_ENABLED", "").strip().lower() in (
    "1",
    "true",
    "yes",
)
# Сколько ₽ «на 1 Star» при переводе из тарифа в ₽ (после пакетной скидки и промокода).
STARS_RUB_PARITY = max(0.01, _env_float("STARS_RUB_PARITY", 2.5))


def _freekassa_rub_amount(months: int) -> float:
    if months <= 0:
        months = 1
    base = FREEKASSA_PRICE_PER_MONTH_RUB * months
    disc = _CRYPTO_PACK_DISCOUNT.get(months, 0.0)
    return float(f"{round(base * (1.0 - disc), 2):.2f}")


def _months_word_ru(n: int) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return "месяц"
    if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return "месяца"
    return "месяцев"


def _money_display_suffix() -> str:
    return "₽" if FREEKASSA_CURRENCY.upper() == "RUB" else FREEKASSA_CURRENCY


def _fmt_tariff_amount(amount: float) -> str:
    rounded = round(amount, 2)
    if abs(rounded - round(rounded)) < 0.001:
        return f"{int(round(rounded))}"
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def _pack_savings_percent(months: int) -> Optional[int]:
    """Скидка пакета от цены «N × месяц» без пакетной скидки (до промокода)."""
    if months <= 1:
        return None
    rack = FREEKASSA_PRICE_PER_MONTH_RUB * months
    if rack <= 0:
        return None
    actual = _freekassa_rub_amount(months)
    pct = int(round(100.0 * (1.0 - float(actual) / float(rack))))
    return max(0, pct) if pct > 0 else None


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

cryptopay_client: Optional[CryptoPayClient] = None
if CRYPTO_PAY_API_TOKEN:
    cryptopay_client = CryptoPayClient(
        CRYPTO_PAY_API_TOKEN,
        testnet=CRYPTO_PAY_TESTNET,
    )


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == ADMIN_TELEGRAM_ID)


def _user_has_list_access(user: User, telegram_user_id: int) -> bool:
    """Доступ к кассе и функциям «в списке» (после инвайта), как у «Оформить подписку»."""
    if telegram_user_id == ADMIN_TELEGRAM_ID:
        return True
    return user.status in {"registered", "awaiting_payment", "active"}


def _referral_commission_pct() -> float:
    """Как в database.credit_referrer_from_buyer_payment (для текста пользователю)."""
    try:
        pct = float(os.getenv("REFERRAL_COMMISSION_PCT", "15").strip())
    except ValueError:
        pct = 15.0
    return max(0.0, min(90.0, pct))


def _invites_per_sub_month() -> int:
    """Как в subscription_fulfillment._invite_slots_for_months (для текста пользователю)."""
    try:
        per = int(os.getenv("INVITES_PER_SUB_MONTH", "3").strip())
    except ValueError:
        per = 3
    return max(1, per)


def _build_referral_program_text(user: User) -> str:
    pct = _referral_commission_pct()
    per_m = _invites_per_sub_month()
    cur = _money_display_suffix()
    lines: list[str] = [
        "<b>Как работает рефералка</b>",
        "",
        "1) Ты заходишь по <b>инвайт-коду</b> друга — он закрепляется как твой пригласивший.",
    ]
    if pct > 0:
        lines.append(
            f"2) Когда ты <b>оплачиваешь подписку</b> (крипта или Stars), "
            f"от суммы в {cur} пригласившему на <b>реф. баланс</b> начисляется "
            f"<b>{pct:g}%</b>."
        )
    else:
        lines.append(
            "2) Процент с оплаты приглашённого сейчас выключен (0%)."
        )
    lines.extend(
        [
            "",
            "3) <b>Реф. баланс</b> можно потратить только на VPN здесь — в кассе "
            "появится кнопка, если баланса хватает на выбранный срок.",
            "",
            f"4) После <b>своей</b> оплаты тебе начисляются инвайты для кнопки "
            f"«Пустить +1»: <b>{per_m}</b> за каждый оплаченный месяц тарифа.",
            "",
            "Оплата <b>с реф. баланса</b> не даёт новый процент пригласившему.",
        ]
    )
    extra: list[str] = []
    if user.inviter_user_id:
        extra.append("У тебя указан пригласивший по инвайту — пункт 2 относится к нему.")
    rb = float(user.referral_balance_rub or 0)
    if rb > 0:
        extra.append(
            f"Твой реф. баланс сейчас: {_fmt_tariff_amount(rb)} {cur}."
        )
    if extra:
        lines.extend(["", "—", "", "\n".join(extra)])
    return "\n".join(lines)


def _nav_btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _subscribe_catalog_text(user: User) -> str:
    """Краткий прайс без рамок и monospace (HTML в панели)."""
    cur = _money_display_suffix()
    lines: list[str] = ["<b>Тарифы</b>", ""]
    if db.resolve_promo_for_checkout(user.id):
        lines.extend(["Промокод учтём в сумме.", ""])
    for m in (1, 3, 6, 12):
        rub, _, _ = _checkout_rub_crypto_promo(user.id, m)
        save = _pack_savings_percent(m)
        human = f"{m} {_months_word_ru(m)}"
        price = _fmt_tariff_amount(rub)
        line = f"{human} — {price}{cur}"
        if save:
            line += f" · −{save}%"
        lines.append(line)
    return "\n".join(lines)


def subscribe_duration_markup(user: User) -> InlineKeyboardMarkup:
    cur = _money_display_suffix()

    def dur_btn(months: int, short_label: str) -> InlineKeyboardButton:
        rub, _, _ = _checkout_rub_crypto_promo(user.id, months)
        return _nav_btn(
            f"{short_label} · {_fmt_tariff_amount(rub)}{cur}",
            f"nav:b:{months}",
        )

    return InlineKeyboardMarkup(
        [
            [dur_btn(1, "1 мес"), dur_btn(3, "3 мес")],
            [dur_btn(6, "6 мес"), dur_btn(12, "12 мес")],
            [_nav_btn("⬅️ Назад в меню", "nav:h")],
        ]
    )


def main_menu_inline_markup(
    is_admin_user: bool,
    *,
    show_support: bool = False,
    show_referral_help: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            _nav_btn("🚷 Статус", "nav:st"),
            _nav_btn("🎟 Кто в списке", "nav:mi"),
        ],
        [
            _nav_btn("➕ Пустить +1", "nav:gi"),
            _nav_btn("💳 Оформить подписку", "nav:buy"),
        ],
        [_nav_btn("🎟 Промокод", "nav:pr")],
    ]
    if is_admin_user:
        rows.insert(
            2,
            [
                _nav_btn("🛠 VIP", "nav:av"),
                _nav_btn("📢 Рассылка", "nav:br"),
            ],
        )
        rows.insert(
            3,
            [
                _nav_btn("📊 Сводка", "nav:as"),
                _nav_btn("🎫 Промокоды", "nav:ap"),
            ],
        )
    if show_referral_help:
        row_rf: list[InlineKeyboardButton] = [
            _nav_btn("🤝 Как работает рефералка", "nav:rf"),
        ]
        if show_support and SUPPORT_TELEGRAM_USERNAME:
            row_rf.append(_nav_btn("💬 Поддержка", "nav:su"))
        rows.append(row_rf)
    elif show_support and SUPPORT_TELEGRAM_USERNAME:
        rows.append([_nav_btn("💬 Поддержка", "nav:su")])
    return InlineKeyboardMarkup(rows)


def _menu_only_markup(is_admin_user: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[_nav_btn("🏠 Открыть меню", "nav:h")]]
    )


def _wizard_cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[_nav_btn("✖ Отмена", "nav:xc"), _nav_btn("🏠 Меню", "nav:h")]]
    )


def _merge_markup(
    base: InlineKeyboardMarkup, extra_rows: list[list[InlineKeyboardButton]]
) -> InlineKeyboardMarkup:
    rows = list(base.inline_keyboard) + extra_rows
    return InlineKeyboardMarkup(rows)


def _clear_ui_wizards(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting_broadcast", None)
    context.user_data.pop("awaiting_promo_apply", None)
    context.user_data.pop("promo_wiz", None)
    context.user_data.pop("admin_promo_field", None)


async def _sync_panel(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    *,
    parse_mode: Optional[str] = None,
) -> None:
    bot = context.bot
    mid = context.user_data.get("panel_mid")
    cid = context.user_data.get("panel_cid") or chat_id
    context.user_data["panel_cid"] = cid
    if mid:
        try:
            await bot.edit_message_text(
                chat_id=cid,
                message_id=mid,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return
        except TelegramError as e:
            err = str(e).lower()
            if "not modified" in err or "message is not modified" in err:
                return
            logger.info("panel edit failed, send new message: %s", e)
    msg = await bot.send_message(
        chat_id=cid,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )
    context.user_data["panel_mid"] = msg.message_id
    context.user_data["panel_cid"] = cid


async def _remove_reply_keyboard(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Снимает нижнюю reply-клавиатуру у клиентов, где она ещё висит со старой версии бота."""
    try:
        m = await context.bot.send_message(
            chat_id=chat_id,
            text="\u2060",
            reply_markup=ReplyKeyboardRemove(),
            disable_notification=True,
        )
        await context.bot.delete_message(chat_id=chat_id, message_id=m.message_id)
    except (BadRequest, TelegramError):
        pass


def _build_home_text(user: User, tg_user_id: int) -> str:
    if user.status == "awaiting_invite":
        return (
            "🚷 Вход только по списку.\n"
            "Напиши **инвайт-код** одним сообщением.\n\n"
            "Кнопки ниже — после входа в список."
        )
    if user.status in {"registered", "awaiting_payment"}:
        return (
            "🎟 Ты в списке.\n"
            "Срок и оплата — «💳 Оформить подписку» в меню."
        )
    if user.status == "active":
        sub = db.get_active_subscription(user.id)
        if sub:
            until = format_access_until(sub["expires_at"])
            return f"🎟 Доступ открыт до:\n{until}\n\nКасса и статус — кнопки ниже."
        if tg_user_id == ADMIN_TELEGRAM_ID:
            return (
                "🛠 Режим владельца: список, инвайты, сводка — кнопки ниже."
            )
        return (
            "В списке ты, срок входа не оформлен.\n"
            "Оформи подписку через «💳 Оформить подписку» или напиши админу."
        )
    return "Касса: выбери действие кнопками ниже."


async def _panel_home(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, tg_user_id: int
) -> None:
    user = db.get_user_by_telegram_id(tg_user_id)
    if not user:
        await _sync_panel(
            context,
            chat_id,
            "Сначала /start",
            main_menu_inline_markup(
                tg_user_id == ADMIN_TELEGRAM_ID,
                show_support=False,
                show_referral_help=False,
            ),
        )
        return
    txt = _build_home_text(user, tg_user_id)
    adm = tg_user_id == ADMIN_TELEGRAM_ID
    show_sup = _user_has_list_access(user, tg_user_id)
    await _sync_panel(
        context,
        chat_id,
        txt,
        main_menu_inline_markup(
            adm, show_support=show_sup, show_referral_help=show_sup
        ),
        parse_mode="Markdown" if user.status == "awaiting_invite" else None,
    )


def _build_status_text(user: User) -> str:
    head_lines = [f"🚷 Статус: {user.status}"]
    if user.inviter_user_id:
        head_lines.append(
            "Ты вошёл по чужому инвайт-коду — при оплате подписки часть суммы уходит пригласившему на реф. баланс."
        )
    sub = db.get_active_subscription(user.id)
    if sub:
        expires_at = datetime.fromisoformat(sub["expires_at"])
        left_days = (expires_at - datetime.now(timezone.utc)).days
        sub_block = "\n".join(
            [
                f"Абонемент: {sub['plan_name']}",
                f"Доступ до: {format_access_until(sub['expires_at'])} (~{left_days} дн.)",
            ]
        )
    else:
        sub_block = "Срок входа не оформлен — всё ещё у двери."
    cur = _money_display_suffix()
    ref_sh = _fmt_tariff_amount(float(user.referral_balance_rub))
    invites_block = "\n".join(
        [
            f"Можешь пускать своих: {'да' if user.can_generate_invites else 'нет'}",
            f"Инвайтов в кармане: {user.invite_balance}",
            f"Реф. баланс (только оплата VPN): {ref_sh} {cur}",
        ]
    )
    blocks: list[str] = ["\n".join(head_lines), sub_block, invites_block]
    if user.marzban_subscription_url:
        blocks.append(
            "Ссылка на подписку (вставьте в приложение):\n\n"
            f"{user.marzban_subscription_url}"
        )
    promo_hint = (user.checkout_promo_code or "").strip()
    if promo_hint:
        blocks.append(f"Промокод к оплате: {promo_hint}")
    return format_telegram_blocks(*blocks)


def _format_admin_stats_text(stats: dict[str, object]) -> str:
    raw_status = stats.get("users_by_status")
    users_by_status: dict[str, int] = (
        raw_status if isinstance(raw_status, dict) else {}
    )
    status_lines = "\n".join(
        f"  • {st}: {cnt}"
        for st, cnt in sorted(users_by_status.items(), key=lambda x: x[0])
    ) or "  • (нет данных)"

    fk_cur = FREEKASSA_CURRENCY
    fk_done_sum = float(stats.get("fk_done_sum") or 0)
    fk_7d_sum = float(stats.get("fk_7d_sum") or 0)
    fk_30d_sum = float(stats.get("fk_30d_sum") or 0)

    blocks = [
        "📊 Сводка",
        f"Пользователей в базе: {stats.get('users_total', 0)}",
        "По статусу:",
        status_lines,
        "",
        "Подписки (таблица subscriptions):",
        f"  • сейчас действуют (срок в будущем): {stats.get('subs_active_valid', 0)}",
        f"  • просрочены, флаг ещё active=1: {stats.get('subs_expired_but_active_flag', 0)}",
        f"  • active=0: {stats.get('subs_inactive_flag', 0)}",
        "",
        f"FreeKassa (завершённые, {fk_cur}):",
        f"  • всего оплат: {stats.get('fk_done_n', 0)} на {fk_done_sum:.2f}",
        f"  • за 7 дней: {stats.get('fk_7d_n', 0)} на {fk_7d_sum:.2f}",
        f"  • за 30 дней: {stats.get('fk_30d_n', 0)} на {fk_30d_sum:.2f}",
        f"  • ожидают оплаты (pending): {stats.get('fk_pending', 0)}",
        "",
        "Crypto Bot:",
        f"  • завершённых оплат: {stats.get('cr_done_n', 0)} "
        f"(всего месяцев выкуплено: {stats.get('cr_months_total', 0)})",
        f"  • за 7 дней: {stats.get('cr_7d_n', 0)}",
        f"  • за 30 дней: {stats.get('cr_30d_n', 0)}",
        f"  • pending счетов: {stats.get('cr_pending', 0)}",
        "",
        "Инвайты:",
        f"  • кодов всего: {stats.get('inv_total', 0)} (root: {stats.get('inv_root', 0)}, "
        f"от юзеров: {stats.get('inv_user_gen', 0)})",
        f"  • кодов с ≥1 использованием: {stats.get('inv_used_codes', 0)}",
        f"  • суммарно списаний по кодам: {stats.get('inv_uses_sum', 0)}",
        "",
        "Прочее:",
        f"  • с выданным Marzban в боте: {stats.get('with_marzban', 0)}",
        f"  • пришли по чужому инвайту (есть inviter): {stats.get('with_inviter', 0)}",
    ]
    return "\n".join(blocks)


def _crypto_amount_for_months(months: int) -> str:
    if months <= 0:
        months = 1
    base = CRYPTO_PRICE_PER_MONTH * months
    disc = _CRYPTO_PACK_DISCOUNT.get(months, 0.0)
    amount = base * (1.0 - disc)
    return f"{amount:.2f}"


def _apply_promo_discount_amount(base: float, discount_percent: float) -> float:
    pct = max(0.0, min(100.0, float(discount_percent)))
    return float(f"{round(base * (1.0 - pct / 100.0), 2):.2f}")


def _checkout_rub_crypto_promo(
    user_internal_id: int, months: int
) -> tuple[float, str, Optional[int]]:
    rub_base = _freekassa_rub_amount(months)
    crypto_base = float(_crypto_amount_for_months(months))
    pr = db.resolve_promo_for_checkout(user_internal_id)
    if pr is None:
        return rub_base, f"{crypto_base:.2f}", None
    d = float(pr["discount_percent"])
    pid = int(pr["id"])
    return (
        _apply_promo_discount_amount(rub_base, d),
        f"{_apply_promo_discount_amount(crypto_base, d):.2f}",
        pid,
    )


def _stars_amount_for_checkout(user_internal_id: int, months: int) -> int:
    rub, _, _ = _checkout_rub_crypto_promo(user_internal_id, months)
    return max(1, int(round(float(rub) / STARS_RUB_PARITY)))


def _parse_stars_payload(payload: str) -> Optional[tuple[int, int, Optional[int]]]:
    """u{internal_id}:m{months}:s[:promo_id] — id пользователя в БД."""
    if ":s:" not in payload:
        return None
    try:
        head, tail = payload.split(":s:", 1)
        if ":m" not in head or not head.startswith("u"):
            return None
        u_part, m_part = head.split(":m", 1)
        uid = int(u_part[1:])
        mn = int(m_part)
        promo: Optional[int] = None
        if tail:
            if not tail.isdigit():
                return None
            p = int(tail)
            if p > 0:
                promo = p
        return uid, mn, promo
    except (ValueError, IndexError):
        return None


async def _deliver_subscription_bundle(
    user: User,
    months: int,
    *,
    plan_prefix: str,
    paid_title_line: str,
    reply_target: Optional[Message] = None,
    bot: Optional[Bot] = None,
    promo_code_id: Optional[int] = None,
    paid_rub_for_referrer: Optional[float] = None,
) -> None:
    """Выдаёт подписку, синк Marzban, шлёт итоговое сообщение (ответом или в личку)."""
    body = deliver_subscription_body(
        db,
        marzban_client,
        ADMIN_TELEGRAM_ID,
        user,
        months,
        plan_prefix,
        paid_title_line,
    )
    db.record_promo_use_on_payment_success(user.id, promo_code_id)
    if paid_rub_for_referrer is not None and paid_rub_for_referrer > 0:
        db.credit_referrer_from_buyer_payment(user.id, paid_rub_for_referrer)
    admin_kb = user.telegram_id == ADMIN_TELEGRAM_ID
    if reply_target is not None:
        await reply_target.reply_text(
            body,
            reply_markup=_menu_only_markup(admin_kb),
        )
    else:
        if bot is None:
            raise RuntimeError("bot required without reply_target")
        await bot.send_message(
            chat_id=user.telegram_id,
            text=body,
            reply_markup=_menu_only_markup(admin_kb),
        )


def _payment_choice_summary(months: int, user_internal_id: int) -> str:
    rub, crypto_amt, promo_id = _checkout_rub_crypto_promo(user_internal_id, months)
    cur = _money_display_suffix()
    rub_l = _fmt_tariff_amount(rub)
    nword = f"{months} {_months_word_ru(months)}"
    bits: list[str] = [f"<b>Оплата · {nword}</b>"]
    if promo_id is not None:
        bits.append("промокод учтён")
    in_auto = PAYMENT_PROVIDER == "auto"
    amounts: list[str] = []
    if in_auto:
        amounts.append(f"{rub_l} {cur}")
    if cryptopay_client and in_auto:
        amounts.append(f"{crypto_amt} {CRYPTO_FIAT}")
    if TELEGRAM_STARS_ENABLED and in_auto:
        amounts.append(f"{_stars_amount_for_checkout(user_internal_id, months)} ★")
    if amounts:
        bits.append(" · ".join(amounts))
    ru = db.get_user_by_id(user_internal_id)
    if ru and float(ru.referral_balance_rub) > 0:
        bits.append(
            f"Реф. баланс: {_fmt_tariff_amount(ru.referral_balance_rub)} {cur} (только VPN)"
        )
    return "\n".join(bits)


def _payment_method_keyboard(
    months: int, user_internal_id: int
) -> Optional[InlineKeyboardMarkup]:
    rows: list[list[InlineKeyboardButton]] = []
    in_auto = PAYMENT_PROVIDER == "auto"
    rub, _, _ = _checkout_rub_crypto_promo(user_internal_id, months)
    buyer = db.get_user_by_id(user_internal_id)
    if (
        buyer is not None
        and in_auto
        and rub > 0
        and float(buyer.referral_balance_rub) + 1e-6 >= float(rub)
    ):
        rows.append(
            [
                InlineKeyboardButton(
                    "💰 Оплата с реф. баланса",
                    callback_data=f"p:r:{months}",
                )
            ]
        )
    top: List[InlineKeyboardButton] = []
    if cryptopay_client and in_auto:
        top.append(
            InlineKeyboardButton("💎 Crypto Bot", callback_data=f"p:c:{months}")
        )
    if top:
        rows.append(top)
    if TELEGRAM_STARS_ENABLED and in_auto:
        rows.append(
            [
                InlineKeyboardButton(
                    "⭐ Telegram Stars", callback_data=f"p:s:{months}"
                )
            ]
        )
    if not rows:
        return None
    return InlineKeyboardMarkup(rows)


def _buy_panel_markup(months: int, user_internal_id: int) -> InlineKeyboardMarkup:
    pay = _payment_method_keyboard(months, user_internal_id)
    rows: list[list[InlineKeyboardButton]] = []
    if pay:
        rows.extend(pay.inline_keyboard)
    rows.append([_nav_btn("⬅️ В меню", "nav:h")])
    return InlineKeyboardMarkup(rows)


async def nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None or q.from_user is None or q.message is None:
        return
    await q.answer()
    data = q.data
    chat_id = q.message.chat_id
    tg_id = q.from_user.id
    adm = tg_id == ADMIN_TELEGRAM_ID

    if data == "nav:xc":
        _clear_ui_wizards(context)
        await _panel_home(context, chat_id, tg_id)
        return

    if data == "nav:h":
        _clear_ui_wizards(context)
        await _panel_home(context, chat_id, tg_id)
        return

    user = db.get_user_by_telegram_id(tg_id)
    if not user:
        await q.answer("Сначала /start", show_alert=True)
        return

    show_sup = _user_has_list_access(user, tg_id)

    if data == "nav:su":
        _clear_ui_wizards(context)
        if not SUPPORT_TELEGRAM_USERNAME:
            await q.answer("Поддержка не настроена.", show_alert=True)
            return
        if not _user_has_list_access(user, tg_id):
            await q.answer(
                "Поддержка доступна после входа в список по инвайту.",
                show_alert=True,
            )
            return
        uname = SUPPORT_TELEGRAM_USERNAME
        link = f"https://t.me/{uname}"
        txt = (
            "💬 Поддержка\n\n"
            f"Напиши @{uname} в личку — поможем с оплатой и доступом."
        )
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Написать в Telegram", url=link)],
                [_nav_btn("« Меню", "nav:h")],
            ]
        )
        await _sync_panel(context, chat_id, txt, kb)
        return

    if data == "nav:rf":
        _clear_ui_wizards(context)
        if not _user_has_list_access(user, tg_id):
            await q.answer(
                "Раздел доступен после входа в список по инвайту.",
                show_alert=True,
            )
            return
        await _sync_panel(
            context,
            chat_id,
            _build_referral_program_text(user),
            InlineKeyboardMarkup([[_nav_btn("« Меню", "nav:h")]]),
            parse_mode="HTML",
        )
        return

    if data == "nav:st":
        _clear_ui_wizards(context)
        txt = _build_status_text(user)
        await _sync_panel(
            context,
            chat_id,
            txt,
            InlineKeyboardMarkup([[_nav_btn("« Меню", "nav:h")]]),
        )
        return

    if data == "nav:mi":
        _clear_ui_wizards(context)
        if not user.can_generate_invites:
            await _sync_panel(
                context,
                chat_id,
                "Пускать своих могут только с абонементом. Сначала оформи оплату.",
                main_menu_inline_markup(
                    adm, show_support=show_sup, show_referral_help=show_sup
                ),
            )
            return
        count = db.get_user_invite_count(user.id)
        txt = (
            f"Уже выписал кодов: {count}\n"
            f"Можешь пустить ещё раз: {user.invite_balance} шт.\n"
            "Новый код — кнопка «Пустить +1»."
        )
        await _sync_panel(
            context,
            chat_id,
            txt,
            main_menu_inline_markup(
                adm, show_support=show_sup, show_referral_help=show_sup
            ),
        )
        return

    if data == "nav:gi":
        _clear_ui_wizards(context)
        if not user.can_generate_invites:
            await _sync_panel(
                context,
                chat_id,
                "Тебе нельзя звать гостей, пока сам не купил вход.",
                main_menu_inline_markup(
                    adm, show_support=show_sup, show_referral_help=show_sup
                ),
            )
            return
        if user.invite_balance <= 0:
            await _sync_panel(
                context,
                chat_id,
                "Инвайты кончились. Продли вход.",
                main_menu_inline_markup(
                    adm, show_support=show_sup, show_referral_help=show_sup
                ),
            )
            return
        if not db.consume_invite_balance(user.id, amount=1):
            await _sync_panel(
                context,
                chat_id,
                "Касса глюканула. Ещё раз «Пустить +1».",
                main_menu_inline_markup(
                    adm, show_support=show_sup, show_referral_help=show_sup
                ),
            )
            return
        code = db.create_invite(
            owner_user_id=user.id,
            created_by_user_id=user.id,
            max_uses=1,
            type_="user_generated",
            expires_days=14,
        )
        await _sync_panel(
            context,
            chat_id,
            f"🎟\n`{code}`",
            main_menu_inline_markup(
                adm, show_support=show_sup, show_referral_help=show_sup
            ),
            parse_mode="Markdown",
        )
        return

    if data == "nav:buy":
        _clear_ui_wizards(context)
        if user.status == "awaiting_invite":
            await q.answer("Сначала инвайт-код текстом.", show_alert=True)
            return
        if user.status not in {"registered", "awaiting_payment", "active"}:
            await q.answer("Сначала пройди вход в список.", show_alert=True)
            return
        await _sync_panel(
            context,
            chat_id,
            _subscribe_catalog_text(user),
            subscribe_duration_markup(user),
            parse_mode="HTML",
        )
        return

    if data.startswith("nav:b:"):
        _clear_ui_wizards(context)
        try:
            months = int(data.split(":")[2])
        except (IndexError, ValueError):
            return
        if months < 1 or months > 36:
            return
        if user.status == "awaiting_invite":
            await q.answer("Сначала инвайт-код текстом.", show_alert=True)
            return
        if user.status not in {"registered", "awaiting_payment", "active"}:
            await q.answer("Сначала пройди вход в список.", show_alert=True)
            return

        if PAYMENT_PROVIDER == "crypto":
            await _send_crypto_invoice(q.message, user, months, is_admin_user=adm)
            return
        pay_kb = _payment_method_keyboard(months, user.id)
        if pay_kb is None:
            _, _, promo_pid = _checkout_rub_crypto_promo(user.id, months)
            await _deliver_subscription_bundle(
                user,
                months,
                plan_prefix="test",
                paid_title_line=f"🎟 Вход на {months} мес. оформлен (тест, без крипты).",
                reply_target=q.message,
                promo_code_id=promo_pid,
            )
            await _panel_home(context, chat_id, tg_id)
            return
        await _sync_panel(
            context,
            chat_id,
            _payment_choice_summary(months, user.id),
            _buy_panel_markup(months, user.id),
            parse_mode="HTML",
        )
        return

    if data == "nav:pr":
        _clear_ui_wizards(context)
        context.user_data["awaiting_promo_apply"] = True
        nu = db.get_user_by_telegram_id(tg_id)
        hint = ""
        if nu and (nu.checkout_promo_code or "").strip():
            hint = f"\nСейчас выбран: {nu.checkout_promo_code}"
        await _sync_panel(
            context,
            chat_id,
            "Пришли код промокода одним сообщением.\n"
            "/promo_clear — сбросить." + hint,
            _wizard_cancel_markup(),
        )
        return

    if not adm:
        await q.answer("Только владелец.", show_alert=True)
        return

    if data == "nav:av":
        _clear_ui_wizards(context)
        code = db.create_invite(
            owner_user_id=None,
            created_by_user_id=None,
            max_uses=1,
            type_="root",
            expires_days=30,
        )
        await _sync_panel(
            context,
            chat_id,
            f"VIP в список (root):\n`{code}`\nактиваций: 1, срок: 30 дн.",
            main_menu_inline_markup(
                True, show_support=True, show_referral_help=True
            ),
            parse_mode="Markdown",
        )
        return

    if data == "nav:br":
        _clear_ui_wizards(context)
        context.user_data["awaiting_broadcast"] = True
        await _sync_panel(
            context,
            chat_id,
            "Рассылка: следующее сообщение (текст/фото/файл) уйдёт всем.\n"
            "/cancel или «Отмена» — выход.",
            _wizard_cancel_markup(),
        )
        return

    if data == "nav:as":
        _clear_ui_wizards(context)
        stats = db.admin_dashboard_stats()
        await _sync_panel(
            context,
            chat_id,
            _format_admin_stats_text(stats),
            InlineKeyboardMarkup([[_nav_btn("« Меню", "nav:h")]]),
        )
        return

    if data == "nav:ap":
        _clear_ui_wizards(context)
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Список промокодов", callback_data="admpr:l")],
                [InlineKeyboardButton("Создать новый", callback_data="admpr:n")],
                [_nav_btn("« Меню", "nav:h")],
            ]
        )
        await _sync_panel(
            context,
            chat_id,
            "Промокоды: скидка от суммы после пакетных скидок.",
            kb,
        )
        return


async def _send_crypto_invoice(
    to_message: Message, user: User, months: int, *, is_admin_user: bool
) -> None:
    if cryptopay_client is None:
        await to_message.reply_text(
            "Crypto Pay не настроен.",
            reply_markup=_menu_only_markup(is_admin_user),
        )
        return
    _, amt, promo_id = _checkout_rub_crypto_promo(user.id, months)
    desc = f"Доступ {months} мес."
    payload = f"u{user.id}:m{months}"
    try:
        inv = await asyncio.to_thread(
            cryptopay_client.create_invoice_fiat,
            amount=amt,
            fiat=CRYPTO_FIAT,
            description=desc,
            payload=payload,
        )
    except CryptoPayError as exc:
        logger.exception("Crypto Pay createInvoice failed")
        await to_message.reply_text(
            format_telegram_blocks(
                "Крипто-касса не ответила. Попробуй позже или напиши админу.",
                str(exc.message),
            ),
            reply_markup=_menu_only_markup(is_admin_user),
        )
        return
    invoice_id = inv.get("invoice_id")
    if invoice_id is None:
        await to_message.reply_text(
            "Счёт не создался — напиши админу.",
            reply_markup=_menu_only_markup(is_admin_user),
        )
        return
    db.add_pending_crypto_invoice(
        int(invoice_id), user.id, months, promo_code_id=promo_id
    )
    pay_url = (
        inv.get("bot_invoice_url")
        or inv.get("mini_app_invoice_url")
        or inv.get("web_app_invoice_url")
    )
    if not pay_url:
        await to_message.reply_text(
            "Счёт создан, но ссылки нет. Открой @CryptoBot → Crypto Pay.",
            reply_markup=_menu_only_markup(is_admin_user),
        )
        return
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("💎 Оплатить в Crypto Bot", url=pay_url)]]
    )
    await to_message.reply_text(
        f"{amt} {CRYPTO_FIAT}",
        reply_markup=kb,
    )


async def _send_stars_invoice(
    to_message: Message, user: User, months: int, *, is_admin_user: bool
) -> None:
    if not TELEGRAM_STARS_ENABLED:
        await to_message.reply_text(
            "Оплата Stars не включена.",
            reply_markup=_menu_only_markup(is_admin_user),
        )
        return
    stars = _stars_amount_for_checkout(user.id, months)
    _, _, promo_id = _checkout_rub_crypto_promo(user.id, months)
    pid_suffix = promo_id if promo_id is not None else 0
    payload = f"u{user.id}:m{months}:s:{pid_suffix}"
    title = f"Доступ {months} мес."
    description = "Подписка VPN"
    try:
        await to_message.reply_invoice(
            title=title,
            description=description,
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice("Подписка VPN", stars)],
            start_parameter=f"vpn{months}m",
        )
    except TelegramError as exc:
        logger.exception("sendInvoice Stars failed: %s", exc)
        await to_message.reply_text(
            "Не удалось выставить счёт в Stars. "
            "Проверь в @BotFather, что для бота включены платежи (Telegram Stars).",
            reply_markup=_menu_only_markup(is_admin_user),
        )


async def payment_method_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None or query.data is None or query.message is None:
        return
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != "p" or parts[1] not in ("c", "s", "r"):
        return
    try:
        months = int(parts[2])
    except ValueError:
        return
    if months < 1 or months > 36:
        return

    tg_user = query.from_user
    if tg_user is None:
        return
    user = db.get_user_by_telegram_id(tg_user.id)
    if not user:
        await query.message.reply_text(
            "Сначала /start.",
            reply_markup=_menu_only_markup(is_admin(update)),
        )
        return
    if user.status == "awaiting_invite":
        await query.message.reply_text(
            "Сначала код в /start.",
            reply_markup=_menu_only_markup(is_admin(update)),
        )
        return

    is_adm = is_admin(update)

    if parts[1] == "r":
        if PAYMENT_PROVIDER == "crypto":
            await query.message.reply_text(
                "Реф. баланс недоступен в режиме только Crypto Pay.",
                reply_markup=_menu_only_markup(is_adm),
            )
            return
        rub, _, promo_pid = _checkout_rub_crypto_promo(user.id, months)
        if rub <= 0:
            await query.message.reply_text(
                "Некорректная сумма.",
                reply_markup=_menu_only_markup(is_adm),
            )
            return
        buyer = db.get_user_by_id(user.id)
        if buyer is None or float(buyer.referral_balance_rub) + 1e-6 < float(rub):
            await query.message.reply_text(
                "Недостаточно средств на реферальном балансе.",
                reply_markup=_menu_only_markup(is_adm),
            )
            return
        if not db.try_deduct_referral_balance(user.id, rub):
            await query.message.reply_text(
                "Не удалось списать баланс. Попробуй ещё раз.",
                reply_markup=_menu_only_markup(is_adm),
            )
            return
        await _deliver_subscription_bundle(
            user,
            months,
            plan_prefix="refbalance",
            paid_title_line=(
                f"🎟 Списано с реф. баланса: {_fmt_tariff_amount(rub)} "
                f"{_money_display_suffix()}."
            ),
            reply_target=query.message,
            promo_code_id=promo_pid,
            paid_rub_for_referrer=None,
        )
        return

    if parts[1] == "s":
        if not TELEGRAM_STARS_ENABLED:
            await query.message.reply_text(
                "Оплата Stars выключена.",
                reply_markup=_menu_only_markup(is_adm),
            )
            return
        if PAYMENT_PROVIDER == "crypto":
            await query.message.reply_text(
                "Сейчас в .env только крипто-режим.",
                reply_markup=_menu_only_markup(is_adm),
            )
            return
        await _send_stars_invoice(
            query.message, user, months, is_admin_user=is_adm
        )
        return

    # Crypto
    if cryptopay_client is None:
        await query.message.reply_text(
            "Crypto Pay не настроен.",
            reply_markup=_menu_only_markup(is_adm),
        )
        return
    await _send_crypto_invoice(query.message, user, months, is_admin_user=is_adm)


async def stars_pre_checkout(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    q = update.pre_checkout_query
    if q is None or not q.invoice_payload:
        return
    if q.currency != "XTR":
        await q.answer(ok=False, error_message="Нужна оплата в Stars.")
        return
    parsed = _parse_stars_payload(q.invoice_payload)
    if parsed is None:
        await q.answer(ok=False, error_message="Счёт недействителен.")
        return
    uid, months, _promo_in_payload = parsed
    pay_user = db.get_user_by_id(uid)
    if pay_user is None or pay_user.telegram_id != q.from_user.id:
        await q.answer(ok=False, error_message="Профиль не совпал.")
        return
    expected = _stars_amount_for_checkout(uid, months)
    if int(q.total_amount) != int(expected):
        await q.answer(
            ok=False, error_message="Сумма устарела — выбери срок заново."
        )
        return
    await q.answer(ok=True)


async def stars_successful_payment(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    msg = update.message
    if msg is None or msg.successful_payment is None:
        return
    sp = msg.successful_payment
    if sp.currency != "XTR":
        return
    parsed = _parse_stars_payload(sp.invoice_payload)
    if parsed is None:
        logger.error("Stars: bad payload %r", sp.invoice_payload)
        return
    uid, months, promo_id = parsed
    user = db.get_user_by_id(uid)
    if user is None or user.telegram_id != msg.from_user.id:
        logger.error("Stars: user mismatch uid=%s", uid)
        return
    rub_paid, _, _ = _checkout_rub_crypto_promo(uid, months)
    try:
        await _deliver_subscription_bundle(
            user,
            months,
            plan_prefix="stars",
            paid_title_line=(
                f"🎟 Вход на {months} мес. оплачен (Telegram Stars)."
            ),
            reply_target=msg,
            promo_code_id=promo_id,
            paid_rub_for_referrer=rub_paid,
        )
    except Exception:
        logger.exception("Stars: выдача подписки не удалась")


async def _crypto_poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not cryptopay_client:
        return
    ids = db.list_pending_crypto_invoice_ids()
    if not ids:
        return
    try:
        invoices = await asyncio.to_thread(cryptopay_client.get_invoices, ids)
    except CryptoPayError:
        logger.exception("Crypto Pay getInvoices rejected")
        return
    except Exception:
        logger.exception("Crypto Pay getInvoices failed")
        return

    bot = context.bot
    for inv in invoices:
        if str(inv.get("status", "")).lower() != "paid":
            continue
        raw_id = inv.get("invoice_id")
        if raw_id is None:
            continue
        try:
            iid = int(raw_id)
        except (TypeError, ValueError):
            continue
        row = db.try_claim_paid_crypto_invoice(iid)
        if row is None:
            continue
        uid = int(row["user_id"])
        months = int(row["months"])
        pay_user = db.get_user_by_id(uid)
        if pay_user is None:
            logger.error("Crypto invoice %s: user id %s not found", iid, uid)
            continue
        promo_pid = None
        if "promo_code_id" in row.keys() and row["promo_code_id"] is not None:
            promo_pid = int(row["promo_code_id"])
        rub_paid, _, _ = _checkout_rub_crypto_promo(uid, months)
        try:
            await _deliver_subscription_bundle(
                pay_user,
                months,
                plan_prefix="crypto",
                paid_title_line=(
                    f"🎟 Вход на {months} мес. подтверждён (оплата в Crypto Bot)."
                ),
                bot=bot,
                promo_code_id=promo_pid,
                paid_rub_for_referrer=rub_paid,
            )
        except Exception:
            logger.exception("Crypto invoice %s: fulfill failed", iid)


async def _post_init_crypto(application: Application) -> None:
    if not cryptopay_client:
        return
    jq = application.job_queue
    if not jq:
        logger.warning(
            "Задан CRYPTO_PAY_API_TOKEN, но JobQueue нет — "
            'установи зависимость: pip install "python-telegram-bot[job-queue]"'
        )
        return
    jq.run_repeating(
        _crypto_poll_job,
        interval=CRYPTO_POLL_INTERVAL,
        first=10,
        name="cryptopay_poll",
    )
    logger.info("Crypto Pay: опрос счетов каждые %s с", CRYPTO_POLL_INTERVAL)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if tg_user is None or update.message is None:
        return
    chat_id = update.effective_chat.id

    if tg_user.id == ADMIN_TELEGRAM_ID:
        context.user_data.pop("awaiting_broadcast", None)

    # После «Очистить историю» в Telegram id панели в чате уже нет — иначе edit падает и меню не показывается.
    context.user_data.pop("panel_mid", None)
    context.user_data.pop("panel_cid", None)

    await _remove_reply_keyboard(context, chat_id)

    user = db.get_user_by_telegram_id(tg_user.id)
    if not user:
        user = db.create_user(
            telegram_id=tg_user.id,
            username=tg_user.username or "",
            first_name=tg_user.first_name or "",
        )

    if tg_user.id == ADMIN_TELEGRAM_ID and user.status == "awaiting_invite":
        db.update_user_status(user.id, "active")
        db.update_user_invite_policy(
            user.id, can_generate_invites=1, invite_generation_limit=9999
        )
        context.user_data["awaiting_invite"] = False
        user = db.get_user_by_telegram_id(tg_user.id)

    if user.status == "awaiting_invite":
        context.user_data["awaiting_invite"] = True

    await _panel_home(context, chat_id, tg_user.id)


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        if update.message:
            await update.message.reply_text(
                "Только для владельца клуба.",
                reply_markup=_menu_only_markup(False),
            )
        return
    if update.message is None:
        return
    stats = db.admin_dashboard_stats()
    await _sync_panel(
        context,
        update.effective_chat.id,
        _format_admin_stats_text(stats),
        InlineKeyboardMarkup([[_nav_btn("« Меню", "nav:h")]]),
    )


def _promo_admin_card_text(pr: sqlite3.Row) -> str:
    note = (pr["note"] or "").strip()
    exp = pr["expires_at"] or "—"
    return (
        f"#{pr['id']} `{pr['code']}`\n"
        f"Скидка: {float(pr['discount_percent']):.1f}%\n"
        f"Активаций: {int(pr['used_count'])}/{int(pr['max_uses'])}\n"
        f"На одного пользователя: ≤{int(pr['per_user_limit'])}\n"
        f"Статус: {'активен' if int(pr['is_active']) else 'выключен'}\n"
        f"Истекает: {exp}\n"
        f"Заметка: {note or '—'}"
    )


def _promo_admin_detail_kb(pid: int, active: bool) -> InlineKeyboardMarkup:
    act_lbl = "Выключить" if active else "Включить"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("% скидки", callback_data=f"admpr:p:{pid}")],
            [
                InlineKeyboardButton(
                    "Лимит активаций", callback_data=f"admpr:m:{pid}"
                )
            ],
            [
                InlineKeyboardButton(
                    "На одного юзера", callback_data=f"admpr:u:{pid}"
                )
            ],
            [InlineKeyboardButton(act_lbl, callback_data=f"admpr:t:{pid}")],
            [InlineKeyboardButton("Удалить", callback_data=f"admpr:d:{pid}")],
            [InlineKeyboardButton("« Список", callback_data="admpr:l")],
        ]
    )


async def admin_promo_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    q = update.callback_query
    if q is None or q.data is None or q.from_user is None:
        return
    if q.from_user.id != ADMIN_TELEGRAM_ID:
        await q.answer("Только владелец.", show_alert=True)
        return
    await q.answer()
    parts = q.data.split(":")
    if len(parts) < 2:
        return
    tag = parts[1]
    msg = q.message

    if tag == "l":
        rows = db.list_promo_codes_admin()
        if not rows:
            txt = "Промокодов пока нет."
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Создать", callback_data="admpr:n")],
                    [InlineKeyboardButton("« Закрыть", callback_data="admpr:b")],
                ]
            )
        else:
            lines = []
            for r in rows[:30]:
                lines.append(
                    f"#{r['id']} {r['code']} — {float(r['discount_percent']):.0f}% "
                    f"({int(r['used_count'])}/{int(r['max_uses'])})"
                )
            txt = "Список:\n" + "\n".join(lines)
            ib: list[list[InlineKeyboardButton]] = [
                [
                    InlineKeyboardButton(
                        str(r["code"])[:28], callback_data=f"admpr:i:{int(r['id'])}"
                    )
                ]
                for r in rows[:24]
            ]
            ib.append([InlineKeyboardButton("Создать", callback_data="admpr:n")])
            ib.append([InlineKeyboardButton("« Закрыть", callback_data="admpr:b")])
            kb = InlineKeyboardMarkup(ib)
        if msg:
            try:
                await msg.edit_text(txt, reply_markup=kb)
            except BadRequest:
                await msg.reply_text(txt, reply_markup=kb)
        return

    if tag == "b":
        if msg:
            await _panel_home(context, msg.chat_id, ADMIN_TELEGRAM_ID)
        return

    if tag == "n":
        context.user_data["promo_wiz"] = {"step": "code"}
        if msg:
            await msg.reply_text(
                "Новый промокод. Шаг 1/4: код (латиница/цифры, до 40 симв.).",
                reply_markup=_menu_only_markup(True),
            )
        return

    if tag == "i" and len(parts) >= 3:
        pid = int(parts[2])
        pr = db.get_promo_by_id(pid)
        if not pr or not msg:
            return
        await msg.edit_text(
            _promo_admin_card_text(pr),
            reply_markup=_promo_admin_detail_kb(pid, bool(int(pr["is_active"]))),
        )
        return

    if tag in ("p", "m", "u") and len(parts) >= 3:
        pid = int(parts[2])
        field = {"p": "pct", "m": "max", "u": "per"}[tag]
        context.user_data["admin_promo_field"] = (field, pid)
        labels = {
            "pct": "процент скидки 0–100",
            "max": "макс. число активаций (целое ≥1)",
            "per": "сколько раз один пользователь может применить (≥1)",
        }
        if msg:
            await msg.reply_text(
                f"Промокод #{pid}: введи новый {labels[field]} одним числом.",
                reply_markup=_menu_only_markup(True),
            )
        return

    if tag == "t" and len(parts) >= 3:
        pid = int(parts[2])
        pr = db.get_promo_by_id(pid)
        if pr:
            newv = 0 if int(pr["is_active"]) else 1
            db.update_promo_code_fields(pid, is_active=newv)
            pr = db.get_promo_by_id(pid)
        if pr and msg:
            await msg.edit_text(
                _promo_admin_card_text(pr),
                reply_markup=_promo_admin_detail_kb(pid, bool(int(pr["is_active"]))),
            )
        return

    if tag == "d" and len(parts) >= 3:
        pid = int(parts[2])
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Да, удалить", callback_data=f"admpr:dd:{pid}"
                    )
                ],
                [InlineKeyboardButton("Отмена", callback_data=f"admpr:i:{pid}")],
            ]
        )
        if msg:
            await msg.edit_text(
                f"Удалить промокод #{pid} безвозвратно?", reply_markup=kb
            )
        return

    if tag == "dd" and len(parts) >= 3:
        pid = int(parts[2])
        db.delete_promo_code(pid)
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Список", callback_data="admpr:l")]]
        )
        if msg:
            await msg.edit_text("Удалено.", reply_markup=kb)
        return


async def promo_clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if tg is None or update.message is None:
        return
    user = db.get_user_by_telegram_id(tg.id)
    if not user:
        await update.message.reply_text("Сначала /start.")
        return
    db.clear_user_checkout_promo(user.id)
    await _panel_home(context, update.effective_chat.id, tg.id)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if tg_user is None or update.message is None:
        return

    user = db.get_user_by_telegram_id(tg_user.id)
    if not user:
        await update.message.reply_text("Сначала /start.")
        return
    await _sync_panel(
        context,
        update.effective_chat.id,
        _build_status_text(user),
        InlineKeyboardMarkup([[_nav_btn("« Меню", "nav:h")]]),
    )


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    message = update.message
    if tg_user is None or message is None:
        return

    user = db.get_user_by_telegram_id(tg_user.id)
    if not user:
        await message.reply_text(
            "Сначала /start — так заведение работает.",
            reply_markup=_menu_only_markup(is_admin(update)),
        )
        return

    if tg_user.id == ADMIN_TELEGRAM_ID:
        context.user_data["awaiting_invite"] = False

    text = (message.text or "").strip()

    if is_admin(update) and context.user_data.get("awaiting_broadcast"):
        if not text:
            await message.reply_text(
                "Пустой текст не шлю. Напиши текст объявления или /cancel.",
                reply_markup=_menu_only_markup(True),
            )
            return
        await admin_broadcast_execute(update, context)
        return

    ap = context.user_data.get("admin_promo_field")
    if is_admin(update) and ap is not None:
        field, pid = ap
        try:
            if field == "pct":
                v = float(text.replace(",", "."))
                if v < 0 or v > 100:
                    raise ValueError
                db.update_promo_code_fields(pid, discount_percent=v)
            elif field == "max":
                v = int(text)
                if v < 1:
                    raise ValueError
                pr = db.get_promo_by_id(pid)
                if pr and int(pr["used_count"]) > v:
                    await message.reply_text(
                        f"Уже использовано {int(pr['used_count'])} раз — "
                        "лимит не меньше этого.",
                        reply_markup=_menu_only_markup(True),
                    )
                    return
                db.update_promo_code_fields(pid, max_uses=v)
            elif field == "per":
                v = int(text)
                if v < 1:
                    raise ValueError
                db.update_promo_code_fields(pid, per_user_limit=v)
            else:
                raise ValueError
        except ValueError:
            await message.reply_text(
                "Некорректное число.",
                reply_markup=_menu_only_markup(True),
            )
            return
        context.user_data.pop("admin_promo_field", None)
        await message.reply_text("Сохранено.", reply_markup=_menu_only_markup(True))
        return

    wiz = context.user_data.get("promo_wiz")
    if is_admin(update) and isinstance(wiz, dict) and wiz.get("step"):
        if wiz["step"] == "code":
            wiz["code"] = text
            wiz["step"] = "pct"
            await message.reply_text(
                "Шаг 2/4: процент скидки (0–100).",
                reply_markup=_menu_only_markup(True),
            )
            return
        elif wiz["step"] == "pct":
            try:
                p = float(text.replace(",", "."))
                if p < 0 or p > 100:
                    raise ValueError
            except ValueError:
                await message.reply_text(
                    "Нужно число 0–100.",
                    reply_markup=_menu_only_markup(True),
                )
                return
            wiz["pct"] = p
            wiz["step"] = "max"
            await message.reply_text(
                "Шаг 3/4: сколько всего активаций (целое ≥1).",
                reply_markup=_menu_only_markup(True),
            )
            return
        elif wiz["step"] == "max":
            try:
                m = int(text)
                if m < 1:
                    raise ValueError
            except ValueError:
                await message.reply_text(
                    "Нужно целое ≥1.",
                    reply_markup=_menu_only_markup(True),
                )
                return
            wiz["max"] = m
            wiz["step"] = "per"
            await message.reply_text(
                "Шаг 4/4: сколько раз один пользователь может применить код (≥1).",
                reply_markup=_menu_only_markup(True),
            )
            return
        elif wiz["step"] == "per":
            try:
                per = int(text)
                if per < 1:
                    raise ValueError
            except ValueError:
                await message.reply_text(
                    "Нужно целое ≥1.",
                    reply_markup=_menu_only_markup(True),
                )
                return
            try:
                pid = db.create_promo_code(
                    wiz["code"],
                    float(wiz["pct"]),
                    int(wiz["max"]),
                    per,
                    note="",
                )
            except sqlite3.IntegrityError:
                context.user_data.pop("promo_wiz", None)
                await message.reply_text(
                    "Такой код уже существует.",
                    reply_markup=_menu_only_markup(True),
                )
                return
            except ValueError as exc:
                context.user_data.pop("promo_wiz", None)
                await message.reply_text(
                    f"Ошибка: {exc}",
                    reply_markup=_menu_only_markup(True),
                )
                return
            context.user_data.pop("promo_wiz", None)
            await message.reply_text(
                f"Готово. Промокод #{pid} создан.",
                reply_markup=_menu_only_markup(True),
            )
            return

    if context.user_data.get("awaiting_promo_apply"):
        ok, res = db.try_set_checkout_promo_for_user(user.id, text)
        context.user_data.pop("awaiting_promo_apply", None)
        await message.reply_text(
            res,
            reply_markup=_menu_only_markup(is_admin(update)),
        )
        await _panel_home(context, message.chat_id, tg_user.id)
        return

    if context.user_data.get("awaiting_invite"):
        code = text.upper()
        ok, reason, _inviter_user_id = db.consume_invite(code, user.id)
        if not ok:
            await message.reply_text(
                f"{reason}\nДругой код или вежливо к админу.",
                reply_markup=_menu_only_markup(is_admin(update)),
            )
            return
        context.user_data["awaiting_invite"] = False
        db.update_user_status(user.id, "awaiting_payment")
        await _panel_home(context, message.chat_id, tg_user.id)
        return

    await message.reply_text(
        "Не понял. Управление — кнопками в панели выше (/start обновит меню).",
        reply_markup=_menu_only_markup(is_admin(update)),
    )


async def my_invites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if tg_user is None:
        return

    user = db.get_user_by_telegram_id(tg_user.id)
    if not user:
        await update.message.reply_text(
            "Сначала жми /start — без этого даже к списку не подойдёшь.",
            reply_markup=_menu_only_markup(is_admin(update)),
        )
        return

    if not user.can_generate_invites:
        await update.message.reply_text(
            "Пускать своих могут только с абонементом. Сначала оформи абонемент.",
            reply_markup=_menu_only_markup(is_admin(update)),
        )
        return

    count = db.get_user_invite_count(user.id)
    await update.message.reply_text(
        f"Уже выписал кодов: {count}\n"
        f"Можешь пустить ещё раз: {user.invite_balance} шт.\n"
        "Новый код — «Пустить +1».",
        reply_markup=_menu_only_markup(is_admin(update)),
    )


async def gen_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if tg_user is None:
        return

    user = db.get_user_by_telegram_id(tg_user.id)
    if not user:
        await update.message.reply_text(
            "Сначала жми /start — без этого даже к списку не подойдёшь.",
            reply_markup=_menu_only_markup(is_admin(update)),
        )
        return

    if not user.can_generate_invites:
        await update.message.reply_text(
            "Тебе нельзя звать гостей, пока сам не купил вход.",
            reply_markup=_menu_only_markup(is_admin(update)),
        )
        return

    if user.invite_balance <= 0:
        await update.message.reply_text(
            "Инвайты кончились. Продли вход — получишь новые места в списке.",
            reply_markup=_menu_only_markup(is_admin(update)),
        )
        return

    if not db.consume_invite_balance(user.id, amount=1):
        await update.message.reply_text(
            "Касса глюканула. Ещё раз «Пустить +1».",
            reply_markup=_menu_only_markup(is_admin(update)),
        )
        return

    code = db.create_invite(
        owner_user_id=user.id,
        created_by_user_id=user.id,
        max_uses=1,
        type_="user_generated",
        expires_days=14,
    )
    await update.message.reply_text(
        f"🎟\n`{code}`",
        parse_mode="Markdown",
        reply_markup=_menu_only_markup(is_admin(update)),
    )


async def buy_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if tg_user is None:
        return

    user = db.get_user_by_telegram_id(tg_user.id)
    if not user:
        await update.message.reply_text(
            "Сначала жми /start — без этого даже к списку не подойдёшь.",
            reply_markup=_menu_only_markup(is_admin(update)),
        )
        return

    if user.status == "awaiting_invite":
        await update.message.reply_text(
            "Сначала код в /start. Без списка касса не пробьёт.",
            reply_markup=_menu_only_markup(is_admin(update)),
        )
        return

    months = 1
    if len(context.args) >= 1 and context.args[0].isdigit():
        months = max(1, int(context.args[0]))

    is_adm = is_admin(update)

    if PAYMENT_PROVIDER == "crypto":
        await _send_crypto_invoice(
            update.message, user, months, is_admin_user=is_adm
        )
        return

    kb = _payment_method_keyboard(months, user.id)
    if kb is None:
        _, _, promo_pid = _checkout_rub_crypto_promo(user.id, months)
        await _deliver_subscription_bundle(
            user,
            months,
            plan_prefix="test",
            paid_title_line=f"🎟 Вход на {months} мес. оформлен (тест, без крипты).",
            reply_target=update.message,
            promo_code_id=promo_pid,
        )
        return

    await _sync_panel(
        context,
        update.effective_chat.id,
        _payment_choice_summary(months, user.id),
        _buy_panel_markup(months, user.id),
        parse_mode="HTML",
    )


async def admin_wipe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Админ: обнулить контекст бота и по возможности снести недавние сообщения в чате."""
    if not is_admin(update):
        await update.message.reply_text(
            "Только для владельца клуба.",
            reply_markup=_menu_only_markup(is_admin(update)),
        )
        return

    msg = update.message
    if msg is None or update.effective_chat is None:
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Сброс чата только в личке с ботом.",
            reply_markup=_menu_only_markup(True),
        )
        return

    full = bool(context.args and context.args[0].lower() == "full")
    if full:
        db.admin_factory_reset_own_rows(ADMIN_TELEGRAM_ID)

    context.user_data.clear()

    chat_id = update.effective_chat.id
    end_id = msg.message_id
    start_id = max(0, end_id - ADMIN_WIPE_LOOKBACK)
    removed = 0
    fail_streak = 0
    for mid in range(end_id, start_id, -1):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            removed += 1
            fail_streak = 0
        except (BadRequest, TelegramError):
            fail_streak += 1
            if fail_streak >= ADMIN_WIPE_FAIL_STREAK_EXIT:
                logger.debug(
                    "admin_wipe: stop after %s delete errors (message_id ~%s)",
                    fail_streak,
                    mid,
                )
                break
        await asyncio.sleep(0.034)

    suffix = (
        "\n\nВ базе твой аккаунт сброшен (`full`): подписка, инвайты с тобой как владельцем, Marzban-поля в боте очищены."
        if full
        else ""
    )
    await context.bot.send_message(
        chat_id,
        "🧹 Контекст бота обнулён. Из чата убрал то, что удалось "
        f"({removed} шт.; дальше API часто отвечает отказом — цикл обрывается). "
        "Старые сообщения чаще всего только вручную: меню чата → «Очистить историю»."
        f"{suffix}",
        reply_markup=_menu_only_markup(True),
    )
    await _panel_home(context, chat_id, ADMIN_TELEGRAM_ID)


async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text(
            "Только для владельца клуба.",
            reply_markup=_menu_only_markup(is_admin(update)),
        )
        return
    context.user_data["awaiting_broadcast"] = True
    await update.message.reply_text(
        "Режим рассылки: **следующее сообщение** уйдёт всем из базы бота "
        "(текст, фото, файл, пересылка и т.п.).\n"
        "/cancel — отмена.",
        parse_mode="Markdown",
        reply_markup=_menu_only_markup(True),
    )


async def admin_broadcast_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None or not is_admin(update):
        return
    recipients = db.list_broadcast_recipient_telegram_ids(
        exclude_telegram_id=ADMIN_TELEGRAM_ID
    )
    ok = 0
    failed = 0
    for uid in recipients:
        try:
            await context.bot.copy_message(
                chat_id=uid,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
            )
            ok += 1
        except TelegramError as e:
            failed += 1
            logger.warning("broadcast to %s: %s", uid, e)
        await asyncio.sleep(0.035)
    context.user_data["awaiting_broadcast"] = False
    await msg.reply_text(
        f"Рассылка завершена.\nДоставлено: {ok}\nНе удалось: {failed} "
        f"(часто — не жали /start или заблокировали бота).\n"
        f"Адресатов в базе (без тебя): {len(recipients)}.",
        reply_markup=_menu_only_markup(True),
    )
    await _panel_home(context, msg.chat_id, ADMIN_TELEGRAM_ID)


async def admin_broadcast_media_router(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not is_admin(update) or not context.user_data.get("awaiting_broadcast"):
        return
    await admin_broadcast_execute(update, context)


async def universal_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_chat is None:
        return
    tg = update.effective_user
    if tg is None:
        return
    _clear_ui_wizards(context)
    await update.message.reply_text("Отменено.")
    await _panel_home(context, update.effective_chat.id, tg.id)


async def admin_create_root_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text(
            "Только для владельца клуба.",
            reply_markup=_menu_only_markup(is_admin(update)),
        )
        return

    max_uses = 1
    expires_days = 30

    if len(context.args) >= 1 and context.args[0].isdigit():
        max_uses = int(context.args[0])
    if len(context.args) >= 2 and context.args[1].isdigit():
        expires_days = int(context.args[1])

    code = db.create_invite(
        owner_user_id=None,
        created_by_user_id=None,
        max_uses=max_uses,
        type_="root",
        expires_days=expires_days,
    )
    await update.message.reply_text(
        f"VIP в список (root):\n`{code}`\n"
        f"активаций: {max_uses}, срок кода: {expires_days} дн.",
        parse_mode="Markdown",
        reply_markup=_menu_only_markup(is_admin(update)),
    )


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN in .env")
    if not ADMIN_TELEGRAM_ID:
        raise RuntimeError("Set ADMIN_TELEGRAM_ID in .env")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_post_init_crypto)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("my_invites", my_invites))
    app.add_handler(CommandHandler("gen_invite", gen_invite))
    app.add_handler(CommandHandler("buy_test", buy_test))
    app.add_handler(CommandHandler("buy", buy_test))
    app.add_handler(CallbackQueryHandler(nav_callback, pattern=r"^nav:"))
    app.add_handler(CallbackQueryHandler(admin_promo_callback, pattern=r"^admpr:"))
    app.add_handler(PreCheckoutQueryHandler(stars_pre_checkout))
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.SUCCESSFUL_PAYMENT,
            stars_successful_payment,
        )
    )
    app.add_handler(
        CallbackQueryHandler(payment_method_callback, pattern=r"^p:[csr]:\d+$")
    )
    app.add_handler(CommandHandler("admin_wipe", admin_wipe))
    app.add_handler(CommandHandler("admin_create_root_invite", admin_create_root_invite))
    app.add_handler(CommandHandler("admin_stats", admin_stats))
    app.add_handler(CommandHandler("promo_clear", promo_clear_cmd))
    app.add_handler(CommandHandler("cancel", universal_cancel))
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & ~filters.COMMAND
            & ~filters.TEXT,
            admin_broadcast_media_router,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
