"""
Toss Payments billing-key (auto-pay) client.

Flow:
  1. Frontend calls Toss SDK `paymentsWidget.requestBillingAuth(...)` or `requestBillingAuth(...)`
     → after card registration, `authKey` and `customerKey` come back as query params on successUrl.
  2. Backend issues a billing key via `issue_billing_key(auth_key, customer_key)`.
  3. Use the issued billingKey with `charge_billing_key(...)` — called for every recurring charge.

Environment variables:
  - TOSS_CLIENT_KEY  : for frontend SDK (BrandPay / billing client key)
  - TOSS_SECRET_KEY  : for backend API authentication

Testing:
  - The Toss docs public test keys work for the SDK UI flow.
  - Real charges require business registration with Toss + your own account keys.
"""
import base64
import logging
import os
import uuid
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Toss Payments docs public test keys — fallback for SDK UI testing when env vars are unset.
# Replace with your own account keys for real payment processing.
_DOCS_TEST_CLIENT_KEY = "test_ck_D5GePWvyJnrK0W0k6q8gLzN97Eoq"
_DOCS_TEST_SECRET_KEY = "test_sk_zXLkKEypNArWmr5nW7eg4nO8AZdyg5lj"

TOSS_API_BASE = "https://api.tosspayments.com"


class BillingError(Exception):
    """Raised when a Toss API call fails. Preserves code/message."""

    def __init__(self, code: str, message: str, http_status: int = 0):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.http_status = http_status


def get_client_key() -> str:
    return os.environ.get("TOSS_CLIENT_KEY") or _DOCS_TEST_CLIENT_KEY


def _secret_key() -> str:
    return os.environ.get("TOSS_SECRET_KEY") or _DOCS_TEST_SECRET_KEY


def _auth_header() -> dict[str, str]:
    """Toss API uses Basic auth with `secret_key:` (including the colon) base64-encoded."""
    raw = f"{_secret_key()}:".encode("utf-8")
    return {"Authorization": f"Basic {base64.b64encode(raw).decode('ascii')}"}


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", **_auth_header()}
    url = f"{TOSS_API_BASE}{path}"
    try:
        res = requests.post(url, json=body, headers=headers, timeout=15)
    except requests.RequestException as e:
        logger.error("Toss API network error: %s | path=%s", e, path)
        raise BillingError("NETWORK_ERROR", "Could not connect to the payment server.")

    try:
        data = res.json()
    except ValueError:
        logger.error("Toss API response parse failed: status=%s body=%s", res.status_code, res.text[:500])
        raise BillingError("INVALID_RESPONSE", "Could not parse the payment server response.", res.status_code)

    if res.status_code >= 400:
        code = data.get("code") or "UNKNOWN_ERROR"
        message = data.get("message") or "An error occurred while processing the payment."
        logger.warning("Toss API error: %s %s | path=%s body=%s", code, message, path, body)
        raise BillingError(code, message, res.status_code)

    return data


def issue_billing_key(auth_key: str, customer_key: str) -> dict[str, Any]:
    """Issue a billingKey from authKey + customerKey.

    Example response:
      {
        "mId": "...",
        "customerKey": "...",
        "authenticatedAt": "...",
        "method": "card",
        "billingKey": "...",
        "card": { "issuerCode": "...", "acquirerCode": "...", "number": "433012******1234",
                  "cardType": "credit", "ownerType": "personal", ... },
        "cardCompany": "Hyundai",
        "cardNumber": "433012******1234"
      }
    """
    return _post(
        "/v1/billing/authorizations/issue",
        {"authKey": auth_key, "customerKey": customer_key},
    )


def charge_billing_key(
    billing_key: str,
    customer_key: str,
    amount: int,
    order_id: str,
    order_name: str,
    customer_email: str | None = None,
    customer_name: str | None = None,
    tax_free_amount: int = 0,
) -> dict[str, Any]:
    """Execute an auto-charge with the billing key.

    Example response:
      {
        "paymentKey": "...",
        "orderId": "...",
        "status": "DONE",
        "totalAmount": 9900,
        "approvedAt": "...",
        "card": {...},
        ...
      }
    """
    body: dict[str, Any] = {
        "customerKey": customer_key,
        "amount": amount,
        "orderId": order_id,
        "orderName": order_name,
        "taxFreeAmount": tax_free_amount,
    }
    if customer_email:
        body["customerEmail"] = customer_email
    if customer_name:
        body["customerName"] = customer_name

    return _post(f"/v1/billing/{billing_key}", body)


def remove_billing_key(billing_key: str, customer_key: str) -> dict[str, Any]:
    """Delete a billing key. Toss does not expose a public invalidate API, so in practice
    we only remove it from our DB. Callers can simply set user.billing_key=None — this
    function is a hook in case Toss adds an API in the future."""
    # No official invalidate endpoint in the Toss API today, so this is a placeholder. Ignore failures.
    return {"customerKey": customer_key, "removed": True}


def make_order_id(prefix: str = "BPA") -> str:
    """Order ID we issue for a payment. Toss orderId allows 6-64 chars, alphanumeric/_/- only."""
    return f"{prefix}-{uuid.uuid4().hex}"


# ── Pricing table ──────────────────────────────────────────────
# Prices include VAT. Yearly = monthly * 10 (2 months free).
PRICE_TABLE: dict[tuple[str, str], int] = {
    ("pro",        "monthly"): 4_900,
    ("pro",        "yearly"):  49_000,
    ("world_tour", "monthly"): 9_900,
    ("world_tour", "yearly"):  99_000,
}

PLAN_LABELS = {
    "pro":        "Pro",
    "world_tour": "World Tour",
}

CYCLE_LABELS = {
    "monthly": "Monthly",
    "yearly":  "Yearly",
}


def get_price(plan: str, cycle: str) -> int | None:
    return PRICE_TABLE.get((plan, cycle))


def order_name(plan: str, cycle: str) -> str:
    """Toss orderName — product name shown on the checkout window, SMS, and receipt."""
    return f"BPA {PLAN_LABELS.get(plan, plan)} {CYCLE_LABELS.get(cycle, cycle)} Subscription"
