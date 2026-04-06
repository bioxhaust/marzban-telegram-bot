import os
import sqlite3
import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


INVITE_ALPHABET = string.ascii_uppercase + string.digits


@dataclass
class User:
    id: int
    telegram_id: int
    username: str
    first_name: str
    status: str
    inviter_user_id: Optional[int]
    can_generate_invites: int
    invite_generation_limit: int
    invite_balance: int
    referral_balance_rub: float
    marzban_username: str
    marzban_subscription_url: str
    checkout_promo_code: str


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL UNIQUE,
                    username TEXT NOT NULL DEFAULT '',
                    first_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'new',
                    inviter_user_id INTEGER,
                    can_generate_invites INTEGER NOT NULL DEFAULT 0,
                    invite_generation_limit INTEGER NOT NULL DEFAULT 0,
                    invite_balance INTEGER NOT NULL DEFAULT 0,
                    marzban_username TEXT NOT NULL DEFAULT '',
                    marzban_subscription_url TEXT NOT NULL DEFAULT '',
                    checkout_promo_code TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(inviter_user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS invites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    owner_user_id INTEGER,
                    created_by_user_id INTEGER,
                    max_uses INTEGER NOT NULL DEFAULT 1,
                    used_count INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    type TEXT NOT NULL DEFAULT 'root',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(owner_user_id) REFERENCES users(id),
                    FOREIGN KEY(created_by_user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL UNIQUE,
                    plan_name TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS crypto_invoices (
                    invoice_id INTEGER NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL,
                    months INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS freekassa_orders (
                    order_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    months INTEGER NOT NULL,
                    amount_rub REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
                """
            )
            # Lightweight migration for existing databases.
            user_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(users)").fetchall()
            }
            if "marzban_username" not in user_columns:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN marzban_username TEXT NOT NULL DEFAULT ''"
                )
            if "marzban_subscription_url" not in user_columns:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN marzban_subscription_url TEXT NOT NULL DEFAULT ''"
                )
            if "invite_balance" not in user_columns:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN invite_balance INTEGER NOT NULL DEFAULT 0"
                )
            if "checkout_promo_code" not in user_columns:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN checkout_promo_code TEXT NOT NULL DEFAULT ''"
                )
            if "referral_balance_rub" not in user_columns:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN referral_balance_rub REAL NOT NULL DEFAULT 0"
                )
            tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "crypto_invoices" not in tables:
                conn.execute(
                    """
                    CREATE TABLE crypto_invoices (
                        invoice_id INTEGER NOT NULL UNIQUE,
                        user_id INTEGER NOT NULL,
                        months INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(user_id) REFERENCES users(id)
                    )
                    """
                )
            if "freekassa_orders" not in tables:
                conn.execute(
                    """
                    CREATE TABLE freekassa_orders (
                        order_id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        months INTEGER NOT NULL,
                        amount_rub REAL NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(user_id) REFERENCES users(id)
                    )
                    """
                )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS promo_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    discount_percent REAL NOT NULL,
                    max_uses INTEGER NOT NULL DEFAULT 1,
                    used_count INTEGER NOT NULL DEFAULT 0,
                    per_user_limit INTEGER NOT NULL DEFAULT 1,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    note TEXT NOT NULL DEFAULT '',
                    expires_at TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS promo_redemptions (
                    promo_code_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    redemption_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (promo_code_id, user_id),
                    FOREIGN KEY (promo_code_id) REFERENCES promo_codes(id),
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )

            tnames = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for tbl in ("freekassa_orders", "crypto_invoices"):
                if tbl not in tnames:
                    continue
                cols = {
                    r["name"]
                    for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()
                }
                if cols and "promo_code_id" not in cols:
                    conn.execute(
                        f"ALTER TABLE {tbl} ADD COLUMN promo_code_id INTEGER"
                    )

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _generate_code(length: int = 10) -> str:
        return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(length))

    def get_user_by_id(self, user_id: int) -> Optional[User]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        return User(
            id=row["id"],
            telegram_id=row["telegram_id"],
            username=row["username"],
            first_name=row["first_name"],
            status=row["status"],
            inviter_user_id=row["inviter_user_id"],
            can_generate_invites=row["can_generate_invites"],
            invite_generation_limit=row["invite_generation_limit"],
            invite_balance=row["invite_balance"],
            referral_balance_rub=float(row["referral_balance_rub"] or 0),
            marzban_username=row["marzban_username"],
            marzban_subscription_url=row["marzban_subscription_url"],
            checkout_promo_code=str(row["checkout_promo_code"] or ""),
        )

    def list_broadcast_recipient_telegram_ids(
        self, exclude_telegram_id: Optional[int] = None
    ) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT telegram_id FROM users ORDER BY id"
            ).fetchall()
        out = [int(r["telegram_id"]) for r in rows]
        if exclude_telegram_id is not None:
            out = [x for x in out if x != exclude_telegram_id]
        return out

    def get_user_by_telegram_id(self, telegram_id: int) -> Optional[User]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
        if not row:
            return None
        return User(
            id=row["id"],
            telegram_id=row["telegram_id"],
            username=row["username"],
            first_name=row["first_name"],
            status=row["status"],
            inviter_user_id=row["inviter_user_id"],
            can_generate_invites=row["can_generate_invites"],
            invite_generation_limit=row["invite_generation_limit"],
            invite_balance=row["invite_balance"],
            referral_balance_rub=float(row["referral_balance_rub"] or 0),
            marzban_username=row["marzban_username"],
            marzban_subscription_url=row["marzban_subscription_url"],
            checkout_promo_code=str(row["checkout_promo_code"] or ""),
        )

    def create_user(
        self,
        telegram_id: int,
        username: str,
        first_name: str,
        status: str = "awaiting_invite",
        inviter_user_id: Optional[int] = None,
        can_generate_invites: int = 0,
        invite_generation_limit: int = 0,
    ) -> User:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO users (
                    telegram_id, username, first_name, status, inviter_user_id,
                    can_generate_invites, invite_generation_limit, invite_balance,
                    marzban_username, marzban_subscription_url, checkout_promo_code,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, '', '', '', ?)
                """,
                (
                    telegram_id,
                    username,
                    first_name,
                    status,
                    inviter_user_id,
                    can_generate_invites,
                    invite_generation_limit,
                    self._now_iso(),
                ),
            )
            user_id = cur.lastrowid
        return User(
            id=user_id,
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            status=status,
            inviter_user_id=inviter_user_id,
            can_generate_invites=can_generate_invites,
            invite_generation_limit=invite_generation_limit,
            invite_balance=0,
            referral_balance_rub=0.0,
            marzban_username="",
            marzban_subscription_url="",
            checkout_promo_code="",
        )

    def credit_referrer_from_buyer_payment(
        self, buyer_internal_id: int, paid_rub: float
    ) -> None:
        """Доля REFERRAL_COMMISSION_PCT от суммы в ₽ — на баланс пригласившего (только оплата деньгами)."""
        try:
            pct = float(os.getenv("REFERRAL_COMMISSION_PCT", "15").strip())
        except ValueError:
            pct = 15.0
        pct = max(0.0, min(90.0, pct))
        paid_rub = float(paid_rub)
        if paid_rub <= 0 or pct <= 0:
            return
        bonus = round(paid_rub * (pct / 100.0), 2)
        if bonus <= 0:
            return
        with self._connect() as conn:
            row = conn.execute(
                "SELECT inviter_user_id FROM users WHERE id = ?",
                (buyer_internal_id,),
            ).fetchone()
            if not row or row["inviter_user_id"] is None:
                return
            inviter_id = int(row["inviter_user_id"])
            if inviter_id == buyer_internal_id:
                return
            conn.execute(
                """
                UPDATE users SET referral_balance_rub = ROUND(
                    COALESCE(referral_balance_rub, 0) + ?, 2
                )
                WHERE id = ?
                """,
                (bonus, inviter_id),
            )

    def try_deduct_referral_balance(self, user_id: int, amount_rub: float) -> bool:
        amount_rub = round(float(amount_rub), 2)
        if amount_rub <= 0:
            return True
        with self._connect() as conn:
            row = conn.execute(
                "SELECT referral_balance_rub FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if not row:
                return False
            bal = float(row["referral_balance_rub"] or 0)
            if bal + 1e-6 < amount_rub:
                return False
            conn.execute(
                """
                UPDATE users SET referral_balance_rub = ROUND(
                    COALESCE(referral_balance_rub, 0) - ?, 2
                )
                WHERE id = ?
                """,
                (amount_rub, user_id),
            )
        return True

    def update_user_status(self, user_id: int, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET status = ? WHERE id = ?",
                (status, user_id),
            )

    def update_user_invite_policy(
        self, user_id: int, can_generate_invites: int, invite_generation_limit: int
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE users
                SET can_generate_invites = ?, invite_generation_limit = ?
                WHERE id = ?
                """,
                (can_generate_invites, invite_generation_limit, user_id),
            )

    def set_user_marzban_profile(
        self, user_id: int, marzban_username: str, marzban_subscription_url: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE users
                SET marzban_username = ?, marzban_subscription_url = ?
                WHERE id = ?
                """,
                (marzban_username, marzban_subscription_url, user_id),
            )

    def add_invite_balance(self, user_id: int, amount: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE users
                SET invite_balance = CASE
                    WHEN invite_balance + ? < 0 THEN 0
                    ELSE invite_balance + ?
                END
                WHERE id = ?
                """,
                (amount, amount, user_id),
            )

    def consume_invite_balance(self, user_id: int, amount: int = 1) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT invite_balance FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if not row:
                return False
            balance = int(row["invite_balance"])
            if balance < amount:
                return False
            conn.execute(
                "UPDATE users SET invite_balance = invite_balance - ? WHERE id = ?",
                (amount, user_id),
            )
            return True

    def create_invite(
        self,
        owner_user_id: Optional[int],
        created_by_user_id: Optional[int],
        max_uses: int = 1,
        type_: str = "user_generated",
        expires_days: Optional[int] = 14,
    ) -> str:
        expires_at = None
        if expires_days is not None:
            expires_at = (datetime.now(timezone.utc) + timedelta(days=expires_days)).isoformat()

        for _ in range(8):
            code = self._generate_code()
            try:
                with self._connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO invites (
                            code, owner_user_id, created_by_user_id, max_uses, used_count,
                            expires_at, is_active, type, created_at
                        ) VALUES (?, ?, ?, ?, 0, ?, 1, ?, ?)
                        """,
                        (
                            code,
                            owner_user_id,
                            created_by_user_id,
                            max_uses,
                            expires_at,
                            type_,
                            self._now_iso(),
                        ),
                    )
                return code
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("Failed to generate unique invite code")

    def consume_invite(self, code: str, new_user_id: int) -> tuple[bool, str, Optional[int]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM invites WHERE code = ?",
                (code.upper().strip(),),
            ).fetchone()
            if not row:
                return False, "Такого кода в списке нет. No entry.", None

            if row["is_active"] != 1:
                return False, "Этот код сняли со списка. Доступ закрыт.", None

            if row["used_count"] >= row["max_uses"]:
                return False, "Код уже использовали. Один код — один доступ.", None

            if row["expires_at"]:
                expires_at = datetime.fromisoformat(row["expires_at"])
                if datetime.now(timezone.utc) > expires_at:
                    return False, "Срок кода вышел. Ищи свежий инвайт.", None

            conn.execute(
                "UPDATE invites SET used_count = used_count + 1 WHERE id = ?",
                (row["id"],),
            )
            conn.execute(
                "UPDATE users SET inviter_user_id = ?, status = 'registered' WHERE id = ?",
                (row["owner_user_id"], new_user_id),
            )
            return True, "Доступ по списку подтверждён.", row["owner_user_id"]

    def get_user_invite_count(self, user_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM invites WHERE owner_user_id = ?",
                (user_id,),
            ).fetchone()
        return int(row["count"])

    def get_active_subscription(self, user_id: int) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM subscriptions
                WHERE user_id = ? AND is_active = 1
                """,
                (user_id,),
            ).fetchone()
        return row

    def grant_subscription(self, user_id: int, plan_name: str, days: int) -> str:
        expires_at_dt = datetime.now(timezone.utc) + timedelta(days=days)
        expires_at = expires_at_dt.isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO subscriptions (user_id, plan_name, expires_at, is_active, created_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    plan_name = excluded.plan_name,
                    expires_at = excluded.expires_at,
                    is_active = 1
                """,
                (user_id, plan_name, expires_at, self._now_iso()),
            )
            conn.execute(
                "UPDATE users SET status = 'active' WHERE id = ?",
                (user_id,),
            )
        return expires_at

    def add_pending_crypto_invoice(
        self,
        invoice_id: int,
        user_id: int,
        months: int,
        promo_code_id: Optional[int] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO crypto_invoices (
                    invoice_id, user_id, months, status, created_at, promo_code_id
                )
                VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (invoice_id, user_id, months, self._now_iso(), promo_code_id),
            )

    def list_pending_crypto_invoice_ids(self) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT invoice_id FROM crypto_invoices WHERE status = 'pending'"
            ).fetchall()
        return [int(r["invoice_id"]) for r in rows]

    def try_claim_paid_crypto_invoice(
        self, invoice_id: int
    ) -> Optional[sqlite3.Row]:
        """Помечает счёт обработанным и возвращает строку, если она была pending (один раз)."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE crypto_invoices
                SET status = 'completed'
                WHERE invoice_id = ? AND status = 'pending'
                """,
                (invoice_id,),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM crypto_invoices WHERE invoice_id = ?",
                (invoice_id,),
            ).fetchone()
        return row

    def delete_crypto_invoices_for_user(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM crypto_invoices WHERE user_id = ?", (user_id,))

    def add_pending_freekassa_order(
        self,
        order_id: str,
        user_id: int,
        months: int,
        amount_rub: float,
        promo_code_id: Optional[int] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO freekassa_orders (
                    order_id, user_id, months, amount_rub, status, created_at,
                    promo_code_id
                )
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (order_id, user_id, months, amount_rub, self._now_iso(), promo_code_id),
            )

    @staticmethod
    def normalize_promo_code(raw: str) -> str:
        return (raw or "").strip().upper()

    def _fetch_eligible_promo_row(
        self, conn: sqlite3.Connection, code: str, user_internal_id: int
    ) -> Optional[sqlite3.Row]:
        row = conn.execute(
            """
            SELECT * FROM promo_codes
            WHERE UPPER(TRIM(code)) = ? AND is_active = 1
            """,
            (code,),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"]:
            exp = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp:
                return None
        if int(row["used_count"]) >= int(row["max_uses"]):
            return None
        per_user = int(row["per_user_limit"])
        r2 = conn.execute(
            """
            SELECT redemption_count FROM promo_redemptions
            WHERE promo_code_id = ? AND user_id = ?
            """,
            (row["id"], user_internal_id),
        ).fetchone()
        used_by = int(r2["redemption_count"]) if r2 else 0
        if used_by >= per_user:
            return None
        return row

    def try_set_checkout_promo_for_user(
        self, user_internal_id: int, raw_code: str
    ) -> tuple[bool, str]:
        code = self.normalize_promo_code(raw_code)
        if not code:
            return False, "Пустой код."
        if len(code) > 64:
            return False, "Код слишком длинный."
        with self._connect() as conn:
            row = self._fetch_eligible_promo_row(conn, code, user_internal_id)
            if not row:
                return False, "Промокод недействителен, истёк или лимит исчерпан."
            conn.execute(
                "UPDATE users SET checkout_promo_code = ? WHERE id = ?",
                (code, user_internal_id),
            )
        pct = float(row["discount_percent"])
        return True, f"Промокод {code} — скидка {pct:.0f}%. Учтётся при следующей оплате."

    def resolve_promo_for_checkout(
        self, user_internal_id: int
    ) -> Optional[sqlite3.Row]:
        user = self.get_user_by_id(user_internal_id)
        if not user:
            return None
        code = self.normalize_promo_code(user.checkout_promo_code)
        if not code:
            return None
        with self._connect() as conn:
            row = self._fetch_eligible_promo_row(conn, code, user_internal_id)
            if row is None:
                conn.execute(
                    "UPDATE users SET checkout_promo_code = '' WHERE id = ?",
                    (user_internal_id,),
                )
            return row

    def clear_user_checkout_promo(self, user_internal_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET checkout_promo_code = '' WHERE id = ?",
                (user_internal_id,),
            )

    def record_promo_use_on_payment_success(
        self, user_internal_id: int, promo_code_id: Optional[int]
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET checkout_promo_code = '' WHERE id = ?",
                (user_internal_id,),
            )
            if promo_code_id is None:
                return
            cur = conn.execute(
                """
                UPDATE promo_codes
                SET used_count = used_count + 1
                WHERE id = ? AND is_active = 1 AND used_count < max_uses
                """,
                (promo_code_id,),
            )
            if cur.rowcount == 0:
                return
            r = conn.execute(
                """
                SELECT redemption_count FROM promo_redemptions
                WHERE promo_code_id = ? AND user_id = ?
                """,
                (promo_code_id, user_internal_id),
            ).fetchone()
            if r:
                conn.execute(
                    """
                    UPDATE promo_redemptions
                    SET redemption_count = redemption_count + 1
                    WHERE promo_code_id = ? AND user_id = ?
                    """,
                    (promo_code_id, user_internal_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO promo_redemptions (promo_code_id, user_id, redemption_count)
                    VALUES (?, ?, 1)
                    """,
                    (promo_code_id, user_internal_id),
                )

    def list_promo_codes_admin(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM promo_codes ORDER BY id DESC"
                ).fetchall()
            )

    def get_promo_by_id(self, promo_id: int) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM promo_codes WHERE id = ?", (promo_id,)
            ).fetchone()

    def create_promo_code(
        self,
        code: str,
        discount_percent: float,
        max_uses: int,
        per_user_limit: int = 1,
        note: str = "",
        expires_at: Optional[str] = None,
    ) -> int:
        code = self.normalize_promo_code(code)
        if not code:
            raise ValueError("empty code")
        pct = max(0.0, min(100.0, float(discount_percent)))
        mu = max(1, int(max_uses))
        pu = max(1, int(per_user_limit))
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO promo_codes (
                    code, discount_percent, max_uses, used_count, per_user_limit,
                    is_active, note, expires_at, created_at
                )
                VALUES (?, ?, ?, 0, ?, 1, ?, ?, ?)
                """,
                (code, pct, mu, pu, note.strip(), expires_at, self._now_iso()),
            )
            return int(cur.lastrowid)

    def update_promo_code_fields(
        self,
        promo_id: int,
        *,
        discount_percent: Optional[float] = None,
        max_uses: Optional[int] = None,
        per_user_limit: Optional[int] = None,
        is_active: Optional[int] = None,
        note: Optional[str] = None,
        expires_at: Optional[str] = None,
        clear_expires: bool = False,
    ) -> None:
        fields: list[str] = []
        vals: list[object] = []
        if discount_percent is not None:
            fields.append("discount_percent = ?")
            vals.append(max(0.0, min(100.0, float(discount_percent))))
        if max_uses is not None:
            fields.append("max_uses = ?")
            vals.append(max(1, int(max_uses)))
        if per_user_limit is not None:
            fields.append("per_user_limit = ?")
            vals.append(max(1, int(per_user_limit)))
        if is_active is not None:
            fields.append("is_active = ?")
            vals.append(1 if int(is_active) else 0)
        if note is not None:
            fields.append("note = ?")
            vals.append(str(note))
        if clear_expires:
            fields.append("expires_at = NULL")
        elif expires_at is not None:
            fields.append("expires_at = ?")
            vals.append(expires_at)
        if not fields:
            return
        vals.append(promo_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE promo_codes SET {', '.join(fields)} WHERE id = ?",
                vals,
            )

    def delete_promo_code(self, promo_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM promo_redemptions WHERE promo_code_id = ?",
                (promo_id,),
            )
            conn.execute("DELETE FROM promo_codes WHERE id = ?", (promo_id,))

    def try_complete_freekassa_order(
        self, order_id: str, paid_amount: float
    ) -> Optional[sqlite3.Row]:
        tol = 0.02
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM freekassa_orders
                WHERE order_id = ? AND status = 'pending'
                """,
                (order_id,),
            ).fetchone()
            if not row:
                return None
            if abs(float(row["amount_rub"]) - float(paid_amount)) > tol:
                return None
            cur = conn.execute(
                """
                UPDATE freekassa_orders
                SET status = 'completed'
                WHERE order_id = ? AND status = 'pending'
                """,
                (order_id,),
            )
            if cur.rowcount == 0:
                return None
        return row

    def admin_dashboard_stats(self) -> dict[str, object]:
        """Сводные метрики для админки (одним запросом к БД по секциям)."""
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        week_ago = (now - timedelta(days=7)).isoformat()
        month_ago = (now - timedelta(days=30)).isoformat()
        with self._connect() as conn:
            users_total = int(
                conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            )
            st_rows = conn.execute(
                "SELECT status, COUNT(*) AS c FROM users GROUP BY status"
            ).fetchall()
            users_by_status: dict[str, int] = {
                str(r["status"]): int(r["c"]) for r in st_rows
            }

            subs_active_valid = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS c FROM subscriptions
                    WHERE is_active = 1 AND expires_at > ?
                    """,
                    (now_iso,),
                ).fetchone()["c"]
            )
            subs_expired_but_active_flag = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS c FROM subscriptions
                    WHERE is_active = 1 AND expires_at <= ?
                    """,
                    (now_iso,),
                ).fetchone()["c"]
            )
            subs_inactive_flag = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM subscriptions WHERE is_active = 0"
                ).fetchone()["c"]
            )

            row = conn.execute(
                """
                SELECT COUNT(*) AS c, COALESCE(SUM(amount_rub), 0) AS s
                FROM freekassa_orders WHERE status = 'completed'
                """
            ).fetchone()
            fk_done_n, fk_done_sum = int(row["c"]), float(row["s"] or 0)

            row = conn.execute(
                """
                SELECT COUNT(*) AS c, COALESCE(SUM(amount_rub), 0) AS s
                FROM freekassa_orders
                WHERE status = 'completed' AND created_at >= ?
                """,
                (week_ago,),
            ).fetchone()
            fk_7d_n, fk_7d_sum = int(row["c"]), float(row["s"] or 0)

            row = conn.execute(
                """
                SELECT COUNT(*) AS c, COALESCE(SUM(amount_rub), 0) AS s
                FROM freekassa_orders
                WHERE status = 'completed' AND created_at >= ?
                """,
                (month_ago,),
            ).fetchone()
            fk_30d_n, fk_30d_sum = int(row["c"]), float(row["s"] or 0)

            fk_pending = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM freekassa_orders WHERE status = 'pending'"
                ).fetchone()["c"]
            )

            cr_done_n = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS c FROM crypto_invoices
                    WHERE status = 'completed'
                    """
                ).fetchone()["c"]
            )
            cr_months_total = int(
                conn.execute(
                    """
                    SELECT COALESCE(SUM(months), 0) AS s
                    FROM crypto_invoices WHERE status = 'completed'
                    """
                ).fetchone()["s"]
                or 0
            )
            cr_7d_n = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS c FROM crypto_invoices
                    WHERE status = 'completed' AND created_at >= ?
                    """,
                    (week_ago,),
                ).fetchone()["c"]
            )
            cr_30d_n = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS c FROM crypto_invoices
                    WHERE status = 'completed' AND created_at >= ?
                    """,
                    (month_ago,),
                ).fetchone()["c"]
            )
            cr_pending = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS c FROM crypto_invoices
                    WHERE status = 'pending'
                    """
                ).fetchone()["c"]
            )

            inv_total = int(
                conn.execute("SELECT COUNT(*) AS c FROM invites").fetchone()["c"]
            )
            inv_root = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM invites WHERE type = 'root'"
                ).fetchone()["c"]
            )
            inv_user_gen = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS c FROM invites
                    WHERE type = 'user_generated'
                    """
                ).fetchone()["c"]
            )
            inv_used_codes = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM invites WHERE used_count > 0"
                ).fetchone()["c"]
            )
            inv_uses_sum = int(
                conn.execute(
                    "SELECT COALESCE(SUM(used_count), 0) AS s FROM invites"
                ).fetchone()["s"]
                or 0
            )

            with_marzban = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS c FROM users
                    WHERE marzban_username != ''
                       OR marzban_subscription_url != ''
                    """
                ).fetchone()["c"]
            )
            with_inviter = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS c FROM users
                    WHERE inviter_user_id IS NOT NULL
                    """
                ).fetchone()["c"]
            )

        return {
            "users_total": users_total,
            "users_by_status": users_by_status,
            "subs_active_valid": subs_active_valid,
            "subs_expired_but_active_flag": subs_expired_but_active_flag,
            "subs_inactive_flag": subs_inactive_flag,
            "fk_done_n": fk_done_n,
            "fk_done_sum": fk_done_sum,
            "fk_7d_n": fk_7d_n,
            "fk_7d_sum": fk_7d_sum,
            "fk_30d_n": fk_30d_n,
            "fk_30d_sum": fk_30d_sum,
            "fk_pending": fk_pending,
            "cr_done_n": cr_done_n,
            "cr_months_total": cr_months_total,
            "cr_7d_n": cr_7d_n,
            "cr_30d_n": cr_30d_n,
            "cr_pending": cr_pending,
            "inv_total": inv_total,
            "inv_root": inv_root,
            "inv_user_gen": inv_user_gen,
            "inv_used_codes": inv_used_codes,
            "inv_uses_sum": inv_uses_sum,
            "with_marzban": with_marzban,
            "with_inviter": with_inviter,
        }

    def admin_factory_reset_own_rows(self, telegram_id: int) -> None:
        """Сброс данных пользователя в БД (только вызывайте для админского telegram_id)."""
        user = self.get_user_by_telegram_id(telegram_id)
        if not user:
            return
        uid = user.id
        with self._connect() as conn:
            conn.execute("DELETE FROM freekassa_orders WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM crypto_invoices WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM subscriptions WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM invites WHERE owner_user_id = ?", (uid,))
            conn.execute(
                """
                UPDATE users SET
                    status = 'active',
                    inviter_user_id = NULL,
                    can_generate_invites = 1,
                    invite_generation_limit = 9999,
                    invite_balance = 0,
                    referral_balance_rub = 0,
                    marzban_username = '',
                    marzban_subscription_url = '',
                    checkout_promo_code = ''
                WHERE id = ?
                """,
                (uid,),
            )
