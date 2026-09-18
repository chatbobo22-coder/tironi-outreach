from email.message import EmailMessage
from email.utils import formataddr, make_msgid
import smtplib
import ssl

from .base import OutboundEmail, SendResult
from ..config import Settings


def build_message(settings: Settings, email: OutboundEmail) -> EmailMessage:
    """Build a standards-friendly multipart marketing message."""
    msg = EmailMessage()
    msg["From"] = formataddr((settings.from_name, settings.from_email))
    msg["To"] = email.to
    msg["Subject"] = email.subject
    msg["Reply-To"] = settings.reply_to or settings.from_email
    msg["Message-ID"] = make_msgid(domain=settings.from_email.split("@")[-1])
    msg["List-Unsubscribe"] = f"<{email.unsubscribe_url}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg["Precedence"] = "bulk"
    msg["X-Outreach-Message-ID"] = str(email.message_id)

    text = email.text.rstrip() + f"\n\nNão deseja mais receber? {email.unsubscribe_url}\n"
    msg.set_content(text)
    if email.html:
        html = email.html.replace("{unsubscribe_url}", email.unsubscribe_url)
        msg.add_alternative(html, subtype="html")
    return msg


class SmtpProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    def send(self, email: OutboundEmail) -> SendResult:
        self.settings.validate_smtp()
        msg = build_message(self.settings, email)
        try:
            smtp_class = smtplib.SMTP_SSL if self.settings.smtp_ssl else smtplib.SMTP
            kwargs = {
                "host": self.settings.smtp_host,
                "port": self.settings.smtp_port,
                "timeout": self.settings.smtp_timeout,
            }
            if self.settings.smtp_ssl:
                kwargs["context"] = ssl.create_default_context()
            with smtp_class(**kwargs) as smtp:
                smtp.ehlo()
                if self.settings.smtp_starttls and not self.settings.smtp_ssl:
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
