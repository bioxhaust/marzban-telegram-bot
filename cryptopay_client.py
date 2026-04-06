"""Клиент Crypto Pay API (@CryptoBot). Документация: https://help.send.tg (Crypto Pay)."""

from __future__ import annotations

from typing import Any, Optional

import requests


class CryptoPayError(Exception):
    def __init__(
        self,
        message: str,
        code: Optional[int] = None,
        raw: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.raw = raw


class CryptoPayClient:
    def __init__(self, api_token: str, *, testnet: bool = False) -> None:
        host = "testnet-pay.crypt.bot" if testnet else "pay.crypt.bot"
        self._url = f"https://{host}/api"
        self._headers = {
            "Crypto-Pay-API-Token": api_token,
            "Content-Type": "application/json",
        }

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        r = requests.post(
            f"{self._url}/{method}",
            json=payload,
            headers=self._headers,
            timeout=45,
        )
        try:
            data: dict[str, Any] = r.json()
        except Exception as exc:
            raise CryptoPayError(
                f"Crypto Pay: не JSON ({r.status_code}): {r.text[:200]}",
                r.status_code,
            ) from exc
        if not data.get("ok"):
            err = data.get("error") or {}
            raise CryptoPayError(
                str(err.get("name") or data),
                err.get("code") if isinstance(err.get("code"), int) else None,
                data,
            )
        result = data.get("result")
        if not isinstance(result, dict):
            return {}
        return result

    def create_invoice_fiat(
        self,
        *,
        amount: str,
        fiat: str,
        description: str,
        payload: str,
    ) -> dict[str, Any]:
        pl = (payload or "")[:4096]
        body: dict[str, Any] = {
            "currency_type": "fiat",
            "fiat": fiat.upper(),
            "amount": amount,
            "description": description[:1024],
            "payload": pl,
        }
        return self._post("createInvoice", body)

    def get_invoices(self, invoice_ids: list[int]) -> list[dict[str, Any]]:
        if not invoice_ids:
            return []

        def _items_from_result(result: dict[str, Any]) -> list[dict[str, Any]]:
            items = result.get("items")
            if isinstance(items, list):
                return [x for x in items if isinstance(x, dict)]
            return []

        joined = ",".join(str(i) for i in invoice_ids)
        res = self._post("getInvoices", {"invoice_ids": joined})
        out = _items_from_result(res)
        if not out:
            res = self._post("getInvoices", {"invoice_ids": invoice_ids})
            out = _items_from_result(res)
        return out
