"""
Notifier - sends HIGH/WATCH alerts to the officers mapped to a river via WhatsApp (Twilio) and/or e-mail.

Honest status handling:
  * channel not configured  -> nothing is attempted and the alert's status stays 'not_sent'
    (the first version wrote 'failed', which looked like a delivery error)
  * attempted and succeeded -> 'sent';  attempted and failed -> 'failed'
Set RG_NOTIFY_DRY_RUN=true to build messages and log them without contacting Twilio/SMTP (tests).
"""
import logging
import os
import smtplib
from email.mime.text import MIMEText

import config

logger = logging.getLogger(__name__)


def _dry_run():
    return os.getenv("RG_NOTIFY_DRY_RUN", "false").lower() == "true"


def whatsapp_configured():
    return bool(config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN and config.TWILIO_WHATSAPP_FROM)


def email_configured():
    return bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD)


def configuration_status():
    return {"whatsapp_configured": whatsapp_configured(), "email_configured": email_configured(), "dry_run": _dry_run()}


def _e164(number):
    n = str(number).strip()
    if n.lower().startswith("whatsapp:"):
        n = n[len("whatsapp:"):]
    return n


def send_whatsapp(to_number, message):
    """Returns True (sent) / False (attempted, failed) / None (channel not configured)."""
    if not whatsapp_configured():
        logger.warning("Twilio not configured - WhatsApp not attempted.")
        return None
    if _dry_run():
        logger.info(f"[dry-run] WhatsApp to {_e164(to_number)}: {message[:80]}...")
        return True
    try:
        from twilio.rest import Client
        Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN).messages.create(
            from_=config.TWILIO_WHATSAPP_FROM, to=f"whatsapp:{_e164(to_number)}", body=message)
        return True
    except Exception as e:
        logger.error(f"WhatsApp send failed: {e}")
        return False


def send_email(to_email, subject, body):
    """Returns True (sent) / False (attempted, failed) / None (channel not configured)."""
    if not email_configured():
        logger.warning("SMTP not configured - e-mail not attempted.")
        return None
    if _dry_run():
        logger.info(f"[dry-run] e-mail to {to_email}: {subject}")
        return True
    try:
        msg = MIMEText(body)
        msg["Subject"], msg["From"], msg["To"] = subject, config.SMTP_USER, to_email
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        logger.error(f"Email send failed: {e}")
        return False


def build_alert_message(alert, river_name):
    return (
        f"RiverGuard Alert - {alert['severity']}\n"
        f"River: {river_name}\n"
        f"Location: {alert.get('lat')}, {alert.get('lng')}\n"
        f"Risk score: {alert.get('confidence')}/100 (rule-based screening score, not a probability)\n"
        f"Reasoning: {alert.get('reasoning_text')}\n"
        f"Detected at: {alert.get('detected_at')}\n"
        f"This is a signal for field verification, not proof of illegal activity.\n"
        f"Full report: RiverGuard dashboard, alert ID {alert.get('id')}"
    )


def notify_officer(alert, river_name, officer):
    """officer: dict with optional 'whatsapp_number' / 'email'. Returns only the statuses that were attempted."""
    message = build_alert_message(alert, river_name)
    statuses = {}
    if officer.get("whatsapp_number"):
        sent = send_whatsapp(officer["whatsapp_number"], message)
        if sent is not None:
            statuses["whatsapp_status"] = "sent" if sent else "failed"
    if officer.get("email"):
        sent = send_email(officer["email"], f"RiverGuard {alert['severity']} Alert", message)
        if sent is not None:
            statuses["email_status"] = "sent" if sent else "failed"
    return statuses
