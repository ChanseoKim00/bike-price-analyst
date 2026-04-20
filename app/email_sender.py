"""
SendGrid 기반 이메일 발송 모듈.

필요한 환경변수:
  SENDGRID_API_KEY    — SendGrid API 키
  SENDGRID_FROM_EMAIL — 발신자 이메일 (SendGrid에서 Verified Sender 등록 필수)

SENDGRID_API_KEY 미설정 시: 개발 환경으로 간주하고 콘솔 로그만 남기고 성공 반환.
"""
import logging
import os

logger = logging.getLogger(__name__)


def send_password_reset_email(to_email: str, reset_url: str) -> bool:
    """
    비밀번호 재설정 링크 메일 발송.
    Returns: True면 (최소한) 발송 루틴 완료, False면 SendGrid 예외 발생.
    """
    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("SENDGRID_FROM_EMAIL")

    if not api_key or not from_email:
        logger.warning(
            "SendGrid 환경변수 미설정 — 콘솔 로그로 대체. to=%s url=%s",
            to_email, reset_url,
        )
        print(f"[EMAIL:DEV] 비밀번호 재설정 링크 → {to_email}\n  {reset_url}")
        return True

    subject = "[Bike Price Analyst] 비밀번호 재설정 안내"
    text_body = (
        f"안녕하세요, Bike Price Analyst입니다.\n\n"
        f"비밀번호 재설정을 요청하셨습니다. 아래 링크를 눌러 새 비밀번호를 설정해주세요.\n"
        f"{reset_url}\n\n"
        f"이 링크는 30분 동안만 유효합니다.\n"
        f"본인이 요청하지 않았다면 이 메일은 무시하셔도 됩니다.\n"
    )
    html_body = f"""\
<!doctype html>
<html lang="ko">
<body style="font-family: -apple-system, Segoe UI, sans-serif; color:#222; line-height:1.6;">
  <p>안녕하세요, <strong>Bike Price Analyst</strong>입니다.</p>
  <p>비밀번호 재설정을 요청하셨습니다. 아래 버튼을 눌러 새 비밀번호를 설정해주세요.</p>
  <p style="margin: 24px 0;">
    <a href="{reset_url}"
       style="background:#3b82f6; color:#fff; padding:12px 24px; border-radius:8px; text-decoration:none; display:inline-block;">
      비밀번호 재설정
    </a>
  </p>
  <p style="color:#666; font-size:13px;">
    버튼이 동작하지 않으면 아래 주소를 브라우저에 붙여넣어 주세요.<br>
    <a href="{reset_url}">{reset_url}</a>
  </p>
  <p style="color:#666; font-size:13px;">
    이 링크는 <strong>30분</strong> 동안만 유효합니다.<br>
    본인이 요청하지 않았다면 이 메일은 무시하셔도 됩니다.
  </p>
</body>
</html>
"""

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        message = Mail(
            from_email=from_email,
            to_emails=to_email,
            subject=subject,
            plain_text_content=text_body,
            html_content=html_body,
        )
        resp = SendGridAPIClient(api_key).send(message)
        logger.info("비밀번호 재설정 메일 발송 완료 — to=%s status=%s", to_email, resp.status_code)
        return True
    except Exception as e:
        logger.error("SendGrid 발송 실패 — to=%s err=%s", to_email, e)
        return False
