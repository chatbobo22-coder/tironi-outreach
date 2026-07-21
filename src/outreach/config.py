from dataclasses import dataclass
import os

from dotenv import load_dotenv

load_dotenv()


def flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "postgresql://cnpj:cnpj@localhost:5432/cnpj")
    smtp_host: str = os.getenv("SMTP_HOST", "smtp-relay.brevo.com")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_username: str = os.getenv("SMTP_USERNAME", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_starttls: bool = flag("SMTP_STARTTLS", "true")
    smtp_timeout: int = int(os.getenv("SMTP_TIMEOUT_SECONDS", "30"))
    from_email: str = os.getenv("OUTREACH_FROM_EMAIL", "")
    from_name: str = os.getenv("OUTREACH_FROM_NAME", "Tironi Tech")
    reply_to: str = os.getenv("OUTREACH_REPLY_TO", "")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    unsubscribe_secret: str = os.getenv("UNSUBSCRIBE_SECRET", "")
    api_key: str = os.getenv("API_KEY", "")
    require_approval: bool = flag("REQUIRE_MANUAL_APPROVAL", "true")
    daily_limit: int = int(os.getenv("DAILY_EMAIL_LIMIT", "30"))
    hourly_limit: int = int(os.getenv("HOURLY_EMAIL_LIMIT", "8"))
    domain_daily_limit: int = int(os.getenv("MAX_PER_DOMAIN_PER_DAY", "2"))
    send_start_hour: int = int(os.getenv("SEND_START_HOUR", "9"))
    send_end_hour: int = int(os.getenv("SEND_END_HOUR", "17"))
    timezone: str = os.getenv("APP_TIMEZONE", "America/Sao_Paulo")
    poll_seconds: int = int(os.getenv("WORKER_POLL_SECONDS", "15"))
    dry_run: bool = flag("DRY_RUN", "true")

    def validate_smtp(self) -> None:
        missing = [
            name
            for name, value in {
                "SMTP_USERNAME": self.smtp_username,
                "SMTP_PASSWORD": self.smtp_password,
                "OUTREACH_FROM_EMAIL": self.from_email,
                "UNSUBSCRIBE_SECRET": self.unsubscribe_secret,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError("Configuração ausente: " + ", ".join(missing))
