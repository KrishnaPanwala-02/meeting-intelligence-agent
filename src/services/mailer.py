"""Outgoing e-mail (SMTP). Disabled unless EMAIL_ENABLED=true."""

import os
import smtplib
from email.message import EmailMessage

from loguru import logger


def email_enabled() -> bool:
    return os.getenv("EMAIL_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def send_email(to: str, subject: str, body: str) -> bool:
    """Send a plain-text e-mail. Returns False (never raises) on failure."""
    if not email_enabled():
        return False
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", "").strip() or user
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError:
        port = 587
    if not host or not sender:
        logger.error("E-mail enabled but SMTP_HOST / SMTP_USER / SMTP_FROM not set")
        return False

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg.set_content(body)
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.starttls()
        with server:
            if user:
                server.login(user, password)
            server.send_message(msg)
        return True
    except Exception as exc:     # never include message content in logs
        logger.error("Could not send e-mail: {}", type(exc).__name__)
        return False


def send_password_reset(to: str, token: str, minutes: int) -> bool:
    base = os.getenv("PASSWORD_RESET_URL", "").strip()
    where = (f"Open this link to choose a new password:\n{base}?token={token}\n"
             if base else
             f"Use this reset token with POST /auth/reset-password:\n{token}\n")
    body = (
        "A password reset was requested for your Meeting Intelligence Agent account.\n\n"
        f"{where}\n"
        f"It is valid for {minutes} minutes and can be used once.\n"
        "If you did not request this, ignore this message; your password is unchanged.\n"
    )
    return send_email(to, "Reset your password", body)
