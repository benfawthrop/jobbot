"""
notifier.py — SMS & Email Notification System
===============================================
Sends you a text or email when:
  - An application is submitted successfully.
  - An error occurs on an application.
  - A run finishes (summary).

SUPPORTED BACKENDS:
  1. Twilio (SMS)    — Recommended for quick text alerts.
  2. SendGrid (email) — Good for detailed summaries with HTML.
  3. SMTP (email)    — Works with Gmail/Outlook; no paid account needed.

You only need to configure one. Set the relevant keys in config.json.
Leave the others empty and they will be skipped.

HOW TWILIO FREE TRIAL WORKS:
  - Sign up at twilio.com (free trial gives ~$15 credit).
  - Get a free phone number.
  - You can only send to verified numbers on the free trial.
  - Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, TWILIO_TO in config.

HOW SMTP (GMAIL) WORKS:
  - Enable "App Passwords" in your Google account security settings.
  - Use your Gmail address and the App Password (not your real password).
  - Set smtp_host: smtp.gmail.com, smtp_port: 587.
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import pyshorteners

import httpx

logger = logging.getLogger(__name__)

def shorten_url(url):
    s = pyshorteners.Shortener()
    return s.tinyurl.short(url)


class Notifier:
    """
    Sends notifications via configured channels.
    Gracefully skips any channel that isn't configured.
    """

    def __init__(self, config: dict, disabled: bool = False):
        self.disabled = disabled
        self.notif_cfg = config.get("notifications", {})

    async def send_success(self, job: dict):
        """Notify that an application was successfully submitted."""
        if self.disabled:
            return
        title = f"Applied: {job['title']} @ {job['company']}"
        body = (
            f"Successfully submitted an application!\n\n"
            f"Position: {job['title']}\n"
            f"Company:  {job['company']}\n"
            f"Location: {job.get('location', 'N/A')}\n"
            f"URL:      {job['url']}"
        )
        await self._dispatch(title, body)

    async def send_error(self, job: dict, error: str):
        """Notify that an application encountered an error."""
        if self.disabled:
            return
        title = f"Error: {job['title']} @ {job['company']}"
        body = (
            f"Application failed.\n\n"
            f"Position: {job['title']}\n"
            f"Company:  {job['company']}\n"
            f"Error:    {error}"
        )
        await self._dispatch(title, body)

    async def send_manual_action(self, job: dict):
        """Notify that an Easy Apply job was found and needs a manual click."""
        if self.disabled:
            return
        title = f"EZ App: {job['title']} @ {job['company']}"
        try:
            link = shorten_url(job['url'])
        except Exception:
            link = job['url']
        body = f"Link: {link}"

        await self._dispatch(title, body)

    async def send_summary(self, results: dict):
        """Send a run summary notification."""
        if self.disabled:
            return
        applied = results.get("applied", [])
        errors = results.get("errors", [])
        title = f"Job Bot Run Complete — {len(applied)} applied"
        body = f"Job Bot finished a run.\n\nApplied: {len(applied)}\nErrors: {len(errors)}\n\n"
        if applied:
            body += "Applied to:\n" + "\n".join(f"  • {j['title']} @ {j['company']}" for j in applied)
        if errors:
            body += "\n\nErrors on:\n" + "\n".join(f"  • {j['title']} @ {j['company']}" for j in errors)
        await self._dispatch(title, body)

    async def _dispatch(self, title: str, body: str):
        """Send via all configured channels."""
        # Try Twilio SMS
        # twilio = self.notif_cfg.get("twilio", {})
        # if all(twilio.get(k) for k in ("account_sid", "auth_token", "from_number", "to_number")):
        #     await self._send_twilio_sms(twilio, f"{title}\n\n{body}")

        # # Try SendGrid email
        # sendgrid = self.notif_cfg.get("sendgrid", {})
        # if all(sendgrid.get(k) for k in ("api_key", "from_email", "to_email")):
        #     await self._send_sendgrid_email(sendgrid, title, body)

        # Try SMTP email
        smtp = self.notif_cfg.get("smtp", {})
        if all(smtp.get(k) for k in ("host", "port", "username", "password", "from_email", "to_email")):
            self._send_smtp_email(smtp, title, body)

    # async def _send_twilio_sms(self, cfg: dict, message: str):
    #     """Send SMS via Twilio REST API."""
    #     try:
    #         # Truncate to SMS length
    #         sms_body = message[:1500]
    #         url = f"https://api.twilio.com/2010-04-01/Accounts/{cfg['account_sid']}/Messages.json"
    #         async with httpx.AsyncClient() as client:
    #             resp = await client.post(
    #                 url,
    #                 data={"From": cfg["from_number"], "To": cfg["to_number"], "Body": sms_body},
    #                 auth=(cfg["account_sid"], cfg["auth_token"]),
    #                 timeout=10,
    #             )
    #             if resp.status_code in (200, 201):
    #                 logger.info("    SMS notification sent via Twilio.")
    #             else:
    #                 logger.warning(f"  Twilio SMS failed: {resp.status_code} {resp.text[:200]}")
    #     except Exception as e:
    #         logger.warning(f"  Twilio error: {e}")

    # async def _send_sendgrid_email(self, cfg: dict, subject: str, body: str):
    #     """Send email via SendGrid API."""
    #     try:
    #         payload = {
    #             "personalizations": [{"to": [{"email": cfg["to_email"]}]}],
    #             "from": {"email": cfg["from_email"]},
    #             "subject": subject,
    #             "content": [{"type": "text/plain", "value": body}],
    #         }
    #         async with httpx.AsyncClient() as client:
    #             resp = await client.post(
    #                 "https://api.sendgrid.com/v3/mail/send",
    #                 json=payload,
    #                 headers={"Authorization": f"Bearer {cfg['api_key']}"},
    #                 timeout=10,
    #             )
    #             if resp.status_code == 202:
    #                 logger.info("  📧 Email notification sent via SendGrid.")
    #             else:
    #                 logger.warning(f"  SendGrid email failed: {resp.status_code}")
    #     except Exception as e:
    #         logger.warning(f"  SendGrid error: {e}")
    #
    def _send_smtp_email(self, cfg: dict, subject: str, body: str):
        """Send email via SMTP (works with Gmail App Passwords)."""
        try:
            msg = MIMEMultipart()
            msg["From"] = cfg["from_email"]
            msg["To"] = cfg["to_email"]
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain"))

            with smtplib.SMTP(cfg["host"], int(cfg["port"])) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(cfg["username"], cfg["password"])
                server.send_message(msg)

            logger.info("  Notification sent via SMTP.")
        except Exception as e:
            logger.warning(f"  SMTP error: {e}")
