from email.message import EmailMessage
from email.utils import formataddr, make_msgid
import smtplib
import ssl

from .base import OutboundEmail, SendResult
from ..config import Settings


class BrevoSmtpProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    def send(self, email: OutboundEmail) -> SendResult:
        self.settings.validate_smtp()
        msg = EmailMessage()
        msg["From"] = formataddr((self.settings.from_name, self.settings.from_email))
        msg["To"] = email.to
        msg["Subject"] = email.subject
        msg["Reply-To"] = self.settings.reply_to or self.settings.from_email
        msg["Message-ID"] = make_msgid(domain=self.settings.from_email.split("@")[-1])
        msg["List-Unsubscribe"] = f"<{email.unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        msg["X-Outreach-Message-ID"] = str(email.message_id)
        msg.set_content(email.text + f"\n\nNão deseja mais receber? {email.unsubscribe_url}")
        try:
            with smtplib.SMTP(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=self.settings.smtp_timeout,
            ) as smtp:
                smtp.ehlo()
                if self.settings.smtp_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                smtp.login(self.settings.smtp_username, self.settings.smtp_password)
                smtp.send_message(msg)
            return SendResult(True, msg["Message-ID"])
        except (smtplib.SMTPException, OSError) as exc:
            return SendResult(False, error=f"{exc.__class__.__name__}: {str(exc)[:200]}")


class DryRunProvider:
    def send(self, email: OutboundEmail) -> SendResult:
        return SendResult(True, f"dry-run-{email.message_id}")
