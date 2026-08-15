"""
Notifier — sends a HIGH/WATCH alert to the officer mapped to a river via
WhatsApp (Twilio) and/or email. Every send is wrapped so a missing/invalid
credential never crashes the pipeline — it just logs and marks the alert's
whatsapp_status/email_status as 'failed', which the dashboard surfaces.
"""
import logging
import smtplib
from email.mime.text import MIMEText

import config

logger = logging.getLogger(__name__)


def send_whatsapp(to_number, message):
    if not (config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN and config.TWILIO_WHATSAPP_FROM):
        logger.warning("Twilio not configured — skipping WhatsApp send. Set TWILIO_* in .env to enable.")
        return False
    try:
        from twilio.rest import Client
        client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
        client.messages.create(
            from_=config.TWILIO_WHATSAPP_FROM,
            to=f"whatsapp:{to_number}",
            body=message,
        )
        return True
    except Exception as e:
        logger.error(f"WhatsApp send failed: {e}")
        return False


def send_email(to_email, subject, body):
    if not (config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD):
        logger.warning("SMTP not configured — skipping email send. Set SMTP_* in .env to enable.")
        return False
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = config.SMTP_USER
        msg["To"] = to_email
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
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
        f"Confidence: {alert.get('confidence')}%\n"
        f"Reasoning: {alert.get('reasoning_text')}\n"
        f"Detected at: {alert.get('detected_at')}\n"
        f"Full report: see RiverGuard dashboard, alert ID {alert.get('id')}"
    )


def notify_officer(alert, river_name, officer):
    """officer: dict with optional 'whatsapp_number' and 'email' keys."""
    message = build_alert_message(alert, river_name)
    statuses = {}

    if officer.get("whatsapp_number"):
        sent = send_whatsapp(officer["whatsapp_number"], message)
        statuses["whatsapp_status"] = "sent" if sent else "failed"

    if officer.get("email"):
        sent = send_email(officer["email"], f"RiverGuard {alert['severity']} Alert", message)
        statuses["email_status"] = "sent" if sent else "failed"

    return statuses
