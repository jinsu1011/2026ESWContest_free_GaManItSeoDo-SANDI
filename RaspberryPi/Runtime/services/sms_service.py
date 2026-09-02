"""Replaceable server-side SMS provider adapters.

The default implementation targets Naver Cloud SENS and uses only the Python
standard library.  Credentials and the destination number are read from the
backend environment; browser code never receives them.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from time import time
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class SMSProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = int(status_code)


@dataclass(frozen=True)
class SMSDelivery:
    provider: str
    request_id: str | None
    sent_at: float


class SMSProvider(Protocol):
    name: str

    def is_configured(self) -> bool:
        ...

    def send(self, *, to: str, message: str) -> SMSDelivery:
        ...


class NaverSensSMSProvider:
    """Naver Cloud SENS SMS provider with a strict no-credential failure."""

    name = "naver_sens"

    def __init__(
        self,
        *,
        access_key: str | None = None,
        secret_key: str | None = None,
        service_id: str | None = None,
        from_number: str | None = None,
        api_base_url: str = "https://sens.apigw.ntruss.com",
        timeout_seconds: float = 8.0,
    ) -> None:
        self.access_key = access_key or ""
        self.secret_key = secret_key or ""
        self.service_id = service_id or ""
        self.from_number = from_number or ""
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        if self.timeout_seconds <= 0:
            raise ValueError("SMS timeout must be positive")

    @classmethod
    def from_env(cls) -> "NaverSensSMSProvider":
        return cls(
            access_key=os.getenv("SMS_ACCESS_KEY"),
            secret_key=os.getenv("SMS_SECRET_KEY"),
            service_id=os.getenv("SMS_SERVICE_ID"),
            from_number=os.getenv("SMS_FROM_NUMBER"),
            api_base_url=os.getenv("SMS_API_BASE_URL", "https://sens.apigw.ntruss.com"),
            timeout_seconds=_float_env("SMS_TIMEOUT_SECONDS", 8.0),
        )

    def is_configured(self) -> bool:
        return all((self.access_key, self.secret_key, self.service_id, self.from_number))

    def send(self, *, to: str, message: str) -> SMSDelivery:
        if not self.is_configured():
            raise SMSProviderError(
                "SMS_NOT_CONFIGURED",
                "SMS provider credentials are not configured",
                status_code=503,
            )
        if not _valid_phone(to):
            raise SMSProviderError("SMS_DESTINATION_INVALID", "configured manager phone is invalid", status_code=503)
        if not message.strip() or len(message) > 2000:
            raise SMSProviderError("SMS_MESSAGE_INVALID", "SMS message is empty or too long", status_code=422)

        uri = f"/sms/v2/services/{quote(self.service_id, safe='')}/messages"
        timestamp = str(int(time() * 1000))
        signature_source = f"POST {uri}\n{timestamp}\n{self.access_key}"
        signature = base64.b64encode(
            hmac.new(
                self.secret_key.encode("utf-8"),
                signature_source.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("ascii")
        payload = {
            "type": "SMS",
            "contentType": "COMM",
            "countryCode": "82",
            "from": self.from_number,
            "content": message,
            "messages": [{"to": to}],
        }
        request = Request(
            f"{self.api_base_url}{uri}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "x-ncp-apigw-timestamp": timestamp,
                "x-ncp-iam-access-key": self.access_key,
                "x-ncp-apigw-signature-v2": signature,
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8") or "{}")
        except HTTPError as error:
            raise SMSProviderError(
                "SMS_PROVIDER_HTTP_ERROR",
                f"SMS provider returned HTTP {error.code}",
                status_code=502,
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise SMSProviderError(
                "SMS_PROVIDER_UNAVAILABLE",
                f"SMS provider network error: {type(error).__name__}",
                status_code=502,
            ) from error
        except (ValueError, UnicodeError) as error:
            raise SMSProviderError(
                "SMS_PROVIDER_INVALID_RESPONSE",
                "SMS provider returned an invalid response",
                status_code=502,
            ) from error

        request_id = body.get("requestId") if isinstance(body, dict) else None
        return SMSDelivery(self.name, str(request_id) if request_id else None, time())


def mask_phone(value: str) -> str:
    digits = "".join(character for character in str(value) if character.isdigit())
    if len(digits) >= 10:
        return f"{digits[:3]}-****-{digits[-4:]}"
    if len(digits) >= 4:
        return f"****-{digits[-4:]}"
    return "****"


def _valid_phone(value: str) -> bool:
    digits = "".join(character for character in str(value) if character.isdigit())
    return 8 <= len(digits) <= 15


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default
