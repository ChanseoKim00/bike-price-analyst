"""
토스페이먼츠 빌링키(자동결제) 클라이언트.

흐름:
  1. 프론트엔드에서 토스 SDK `paymentsWidget.requestBillingAuth(...)` 또는 `requestBillingAuth(...)`
     호출 → 카드 등록 후 successUrl로 authKey와 customerKey가 쿼리 파라미터로 돌아옴.
  2. 백엔드에서 `issue_billing_key(auth_key, customer_key)`로 빌링키 발급.
  3. 발급된 billingKey로 `charge_billing_key(...)` — 자동결제 매번 호출.

환경변수:
  - TOSS_CLIENT_KEY  : 프론트엔드 SDK용 (브랜드페이/빌링용 클라이언트키)
  - TOSS_SECRET_KEY  : 백엔드 API 인증용

테스트:
  - 토스 docs 공개 테스트키로 SDK UI까지는 동작.
  - 실제 결제는 토스에 사업자 등록 + 본인 계정 키 필요.
"""
import base64
import logging
import os
import uuid
from typing import Any

import requests

logger = logging.getLogger(__name__)

# 토스페이먼츠 docs 공개 테스트 키 — 환경변수 미설정 시 SDK UI 테스트용 폴백.
# 실제 결제 처리에는 본인 계정 키로 교체 필요.
_DOCS_TEST_CLIENT_KEY = "test_ck_D5GePWvyJnrK0W0k6q8gLzN97Eoq"
_DOCS_TEST_SECRET_KEY = "test_sk_zXLkKEypNArWmr5nW7eg4nO8AZdyg5lj"

TOSS_API_BASE = "https://api.tosspayments.com"


class BillingError(Exception):
    """토스 API 호출 실패 시 발생. code/message 보존."""

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
    """토스 API는 `secret_key:` (콜론까지 포함) 를 base64 인코딩한 Basic 인증 사용."""
    raw = f"{_secret_key()}:".encode("utf-8")
    return {"Authorization": f"Basic {base64.b64encode(raw).decode('ascii')}"}


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", **_auth_header()}
    url = f"{TOSS_API_BASE}{path}"
    try:
        res = requests.post(url, json=body, headers=headers, timeout=15)
    except requests.RequestException as e:
        logger.error("Toss API 네트워크 오류: %s | path=%s", e, path)
        raise BillingError("NETWORK_ERROR", "결제 서버에 연결할 수 없습니다.")

    try:
        data = res.json()
    except ValueError:
        logger.error("Toss API 응답 파싱 실패: status=%s body=%s", res.status_code, res.text[:500])
        raise BillingError("INVALID_RESPONSE", "결제 서버 응답을 해석할 수 없습니다.", res.status_code)

    if res.status_code >= 400:
        code = data.get("code") or "UNKNOWN_ERROR"
        message = data.get("message") or "결제 처리 중 오류가 발생했습니다."
        logger.warning("Toss API 오류: %s %s | path=%s body=%s", code, message, path, body)
        raise BillingError(code, message, res.status_code)

    return data


def issue_billing_key(auth_key: str, customer_key: str) -> dict[str, Any]:
    """authKey + customerKey → billingKey 발급.

    응답 예:
      {
        "mId": "...",
        "customerKey": "...",
        "authenticatedAt": "...",
        "method": "카드",
        "billingKey": "...",
        "card": { "issuerCode": "...", "acquirerCode": "...", "number": "433012******1234",
                  "cardType": "신용", "ownerType": "개인", ... },
        "cardCompany": "현대",
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
    """빌링키로 자동결제 실행.

    응답 예:
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
    """빌링키 삭제. 토스에선 별도 invalidate API가 공개되지 않아 실제로는 우리 DB에서만 제거.
    호출자는 user.billing_key=None으로 설정하면 충분 — 본 함수는 향후 API 추가에 대비한 hook."""
    # 현재 toss API에 공식 invalidate 엔드포인트가 없어 placeholder. 실패해도 무시.
    return {"customerKey": customer_key, "removed": True}


def make_order_id(prefix: str = "BPA") -> str:
    """우리가 발급하는 결제 주문 ID. 토스 orderId는 6~64자, 영숫자/_/-만 허용."""
    return f"{prefix}-{uuid.uuid4().hex}"


# ── 요금제 가격표 ──────────────────────────────────────────────
# 부가세 포함 가격. 연간은 월간 * 10 (2달 무료).
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
    "monthly": "월간",
    "yearly":  "연간",
}


def get_price(plan: str, cycle: str) -> int | None:
    return PRICE_TABLE.get((plan, cycle))


def order_name(plan: str, cycle: str) -> str:
    """토스 orderName — 결제창/문자/영수증에 표시되는 상품명."""
    return f"BPA {PLAN_LABELS.get(plan, plan)} {CYCLE_LABELS.get(cycle, cycle)} 구독"
