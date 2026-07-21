import logging
import time

from .config import Settings
from .db import Database
from .providers.base import OutboundEmail
from .providers.brevo_smtp import BrevoSmtpProvider, DryRunProvider
from .security import unsubscribe_token
from .service import inside_send_window

log = logging.getLogger(__name__)


def process_one(conn, settings: Settings) -> bool:
    if not inside_send_window(settings):
        return False
    with conn.transaction():
        row = conn.execute(
            """
            SELECT m.* FROM outreach.messages m
            WHERE m.status IN ('approved','queued')
              AND (m.scheduled_at IS NULL OR m.scheduled_at <= now())
              AND NOT EXISTS (
                SELECT 1 FROM outreach.suppressions s
                WHERE s.channel=m.channel AND lower(s.destination)=lower(m.destination)
              )
              AND (SELECT count(*) FROM outreach.messages x
                   WHERE x.sent_at >= date_trunc('day', now())
                   AND x.status IN ('sent','delivered')) < %s
              AND (SELECT count(*) FROM outreach.messages x
                   WHERE x.sent_at >= now() - interval '1 hour'
                   AND x.status IN ('sent','delivered')) < %s
              AND (SELECT count(*) FROM outreach.messages x
                   WHERE x.destination_domain=m.destination_domain
                   AND x.sent_at >= date_trunc('day', now())
                   AND x.status IN ('sent','delivered')) < %s
            ORDER BY m.scheduled_at NULLS FIRST, m.id
            FOR UPDATE SKIP LOCKED LIMIT 1
            """,
            (settings.daily_limit, settings.hourly_limit, settings.domain_daily_limit),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE outreach.messages SET status='sending',attempt_count=attempt_count+1,updated_at=now() WHERE id=%s",
            (row["id"],),
        )
    token = unsubscribe_token(row["id"], row["destination"], settings.unsubscribe_secret)
    url = f"{settings.public_base_url}/unsubscribe/{row['id']}?token={token}"
    provider_name = "dry_run" if settings.dry_run else "brevo_smtp"
    provider = DryRunProvider() if settings.dry_run else BrevoSmtpProvider(settings)
    result = provider.send(
        OutboundEmail(row["id"], row["destination"], row["subject"] or "", row["body_text"], url)
    )
    if result.accepted:
        conn.execute(
            "UPDATE outreach.messages SET status='sent',provider=%s,provider_message_id=%s,sent_at=now(),updated_at=now() WHERE id=%s",
            (provider_name, result.provider_message_id, row["id"]),
        )
        conn.execute(
            "UPDATE outreach.leads SET status='contacted',updated_at=now() WHERE id=%s",
            (row["lead_id"],),
        )
    else:
        conn.execute(
            "UPDATE outreach.messages SET status='failed',last_error=%s,updated_at=now() WHERE id=%s",
            (result.error, row["id"]),
        )
    conn.commit()
    return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings, db = Settings(), Database(Settings().database_url)
    while True:
        try:
            with db.connect() as conn:
                worked = process_one(conn, settings)
        except Exception:
            log.exception("Falha no worker")
            worked = False
        time.sleep(1 if worked else settings.poll_seconds)


if __name__ == "__main__":
    main()
