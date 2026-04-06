import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests


class MarzbanClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        subscription_url_prefix: str = "",
        inbound_tags: Optional[list[str]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.subscription_url_prefix = subscription_url_prefix.rstrip("/")
        self.inbound_tags = inbound_tags or []
        self._access_token: Optional[str] = None

    def _headers(self) -> dict[str, str]:
        if not self._access_token:
            self.login()
        return {"Authorization": f"Bearer {self._access_token}"}

    def login(self) -> None:
        response = requests.post(
            f"{self.base_url}/api/admin/token",
            data={"username": self.username, "password": self.password},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("Marzban login failed: no access_token in response")
        self._access_token = token

    @staticmethod
    def _build_marzban_username(telegram_id: int) -> str:
        """Стабильное имя без суффикса, если нет Telegram @username."""
        return f"tg{telegram_id}"

    @staticmethod
    def _sanitize_username(value: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", value.strip())
        cleaned = re.sub(r"_+", "_", cleaned).strip("_")
        return cleaned[:32]

    def _build_preferred_username(
        self, telegram_id: int, telegram_username: Optional[str]
    ) -> str:
        if telegram_username:
            candidate = self._sanitize_username(telegram_username)
            if len(candidate) >= 3:
                return candidate
        return self._build_marzban_username(telegram_id)

    @staticmethod
    def _exp_unix(days: int) -> int:
        dt = datetime.now(timezone.utc) + timedelta(days=days)
        return int(dt.timestamp())

    @staticmethod
    def _raise_verbose_http_error(response: requests.Response) -> None:
        try:
            body = response.json()
        except Exception:
            body = response.text
        raise RuntimeError(
            f"Marzban API error {response.status_code} on {response.request.method} "
            f"{response.url}: {body}"
        )

    def _resolve_vless_inbounds(self) -> list[str]:
        if self.inbound_tags:
            return self.inbound_tags

        resp = requests.get(
            f"{self.base_url}/api/inbounds",
            headers=self._headers(),
            timeout=20,
        )
        if resp.status_code >= 400:
            return []

        try:
            payload = resp.json()
        except Exception:
            return []

        tags: list[str] = []
        if isinstance(payload, dict):
            # Common format: {"VLESS": ["tag1", "tag2"], ...}
            for key, value in payload.items():
                if str(key).upper() == "VLESS" and isinstance(value, list):
                    for item in value:
                        if isinstance(item, str) and item.strip():
                            tags.append(item.strip())
                        elif isinstance(item, dict):
                            tag = str(item.get("tag", "")).strip()
                            if tag:
                                tags.append(tag)

                # Alternate format: {"inbounds": [{"protocol":"vless","tag":"..."}]}
                if key == "inbounds" and isinstance(value, list):
                    for item in value:
                        if not isinstance(item, dict):
                            continue
                        protocol = str(item.get("protocol", "")).upper()
                        tag = str(item.get("tag", "")).strip()
                        if protocol == "VLESS" and tag:
                            tags.append(tag)
        elif isinstance(payload, list):
            # Alternate list-only format.
            for item in payload:
                if not isinstance(item, dict):
                    continue
                protocol = str(item.get("protocol", "")).upper()
                tag = str(item.get("tag", "")).strip()
                if protocol == "VLESS" and tag:
                    tags.append(tag)

        deduped: list[str] = []
        seen = set()
        for tag in tags:
            if tag in seen:
                continue
            seen.add(tag)
            deduped.append(tag)
        return deduped

    def _put_user(
        self,
        username: str,
        expire: int,
        data_limit: int,
        vless_inbounds: list[str],
    ) -> requests.Response:
        update_payload: dict[str, Any] = {
            "status": "active",
            "expire": expire,
            "data_limit": data_limit,
        }
        if vless_inbounds:
            update_payload["inbounds"] = {"vless": vless_inbounds}
        return requests.put(
            f"{self.base_url}/api/user/{username}",
            json=update_payload,
            headers=self._headers(),
            timeout=20,
        )

    def create_or_update_user(
        self,
        marzban_username: Optional[str],
        telegram_id: int,
        telegram_username: Optional[str],
        days: int,
        data_limit_gb: int = 0,
    ) -> dict[str, Any]:
        username = marzban_username or self._build_preferred_username(
            telegram_id, telegram_username
        )
        expire = self._exp_unix(days)
        data_limit = data_limit_gb * 1024 * 1024 * 1024 if data_limit_gb > 0 else 0
        vless_inbounds = self._resolve_vless_inbounds()

        # First try create with requested username.
        create_resp = requests.post(
            f"{self.base_url}/api/user",
            json={
                "username": username,
                "status": "active",
                "expire": expire,
                "data_limit": data_limit,
                "proxies": {"vless": {"id": str(uuid.uuid4()), "flow": ""}},
                **({"inbounds": {"vless": vless_inbounds}} if vless_inbounds else {}),
            },
            headers=self._headers(),
            timeout=20,
        )

        if create_resp.status_code in (200, 201):
            user_data = create_resp.json()
        elif create_resp.status_code == 409:
            # Пользователь уже есть в Marzban — только обновляем, без суффиксов в имени.
            update_resp = self._put_user(
                username, expire, data_limit, vless_inbounds
            )
            if update_resp.status_code >= 400:
                self._raise_verbose_http_error(update_resp)
            user_data = update_resp.json()
        else:
            self._raise_verbose_http_error(create_resp)

        sub_url = user_data.get("subscription_url")
        if not sub_url and self.subscription_url_prefix:
            token = user_data.get("subscription_token")
            if token:
                sub_url = f"{self.subscription_url_prefix}/sub/{token}"

        return {
            "username": username,
            "subscription_url": sub_url or "",
            "raw": user_data,
        }
