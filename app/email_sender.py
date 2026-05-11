"""
SendGrid-based email sending module.

Required environment variables:
  SENDGRID_API_KEY    - SendGrid API key
  SENDGRID_FROM_EMAIL - sender email (must be a Verified Sender on SendGrid)

If SENDGRID_API_KEY is not set: treat as development environment, log to console only, and return success.
"""
import logging
import os

logger = logging.getLogger(__name__)


def send_password_reset_email(to_email: str, reset_url: str) -> bool:
    """
    Send a password reset link email.
    Returns: True if the send routine completed (at minimum), False if SendGrid raised an exception.
    """
    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("SENDGRID_FROM_EMAIL")

    if not api_key or not from_email:
        logger.warning(
            "SendGrid env vars not set - falling back to console log. to=%s url=%s",
            to_email, reset_url,
        )
        print(f"[EMAIL:DEV] Password reset link -> {to_email}\n  {reset_url}")
        return True

    subject = "Reset your BPA password"
    text_body = (
        f"Hello, this is Bike Price Analyst.\n\n"
        f"You requested a password reset. Click the link below to set a new password.\n"
        f"{reset_url}\n\n"
        f"This link is valid for 30 minutes only.\n"
        f"If you did not make this request, you can safely ignore this email.\n"
    )
    html_body = f"""\
<!doctype html>
<html lang="en">
<body style="font-family: -apple-system, Segoe UI, sans-serif; color:#222; line-height:1.6;">
  <p>Hello, this is <strong>Bike Price Analyst</strong>.</p>
  <p>You requested a password reset. Click the button below to set a new password.</p>
  <p style="margin: 24px 0;">
    <a href="{reset_url}"
       style="background:#3b82f6; color:#fff; padding:12px 24px; border-radius:8px; text-decoration:none; display:inline-block;">
      Reset password
    </a>
  </p>
  <p style="color:#666; font-size:13px;">
    If the button does not work, copy and paste the URL below into your browser.<br>
    <a href="{reset_url}">{reset_url}</a>
  </p>
  <p style="color:#666; font-size:13px;">
    This link is valid for <strong>30 minutes</strong> only.<br>
    If you did not make this request, you can safely ignore this email.
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
        logger.info("Password reset email sent - to=%s status=%s", to_email, resp.status_code)
        return True
    except Exception as e:
        logger.error("SendGrid send failed - to=%s err=%s", to_email, e)
        return False
