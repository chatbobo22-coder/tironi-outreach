import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Settings
from .db import Database
from .providers.base import OutboundEmail
from .providers.brevo_smtp import BrevoSmtpProvider, DryRunProvider
from .security import unsubscribe_token
from .service import inside_send_window

log = logging.getLogger(__name__)
DAILY_QUOTA_LOCK_ID = 947216300


def local_day_start(settings: Settings) -> datetime:
    now = datetime.now(ZoneInfo(settings.timezone))
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def process_one_result(
    conn,
    settings: Settings,
    campaign_id: int | None = None,
) -> str | None:
    if not inside_send_window(settings):
        return None
    day_start = local_day_start(settings)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (DAILY_QUOTA_LOCK_ID,))
        row = conn.execute(
            """
            SELECT m.* FROM outreach.messages m
            JOIN outreach.leads l ON l.id=m.lead_id
            WHERE m.status IN ('approved','queued')
              AND (m.scheduled_at IS NULL OR m.scheduled_at <= now())
              AND (
                (m.sequence_step=0 AND l.status='ready')
                OR (m.sequence_step=1 AND l.status='contacted')
              )
              AND NOT EXISTS (
                SELECT 1 FROM outreach.suppressions s
                WHERE s.channel=m.channel AND lower(s.destination)=lower(m.destination)
              )
              AND (SELECT count(*) FROM outreach.messages x
                   WHERE x.sent_at >= %s
                      OR (x.status IN ('sending','delivery_uncertain')
                          AND x.updated_at >= %s)) < %s
              AND (SELECT count(*) FROM outreach.messages x
                   WHERE x.sent_at >= now() - interval '1 hour'
                      OR (x.status IN ('sending','delivery_uncertain')
                          AND x.updated_at >= now() - interval '1 hour')) < %s
              AND (SELECT count(*) FROM outreach.messages x
                   WHERE x.destination_domain=m.destination_domain
                     AND (x.sent_at >= %s
                          OR (x.status IN ('sending','delivery_uncertain')
                              AND x.updated_at >= %s))) < %s
              AND (%s IS NULL OR m.campaign_id=%s)
            ORDER BY m.scheduled_at NULLS FIRST, m.id
            FOR UPDATE SKIP LOCKED LIMIT 1
            """,
            (
                day_start,
                day_start,
                settings.daily_limit,
                settings.hourly_limit,
                day_start,
                day_start,
                settings.domain_daily_limit,
                campaign_id,
                campaign_id,
            ),
        ).fetchone()
        if not row:
            return None
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
            """
            UPDATE outreach.leads
            SET status='contacted',updated_at=now()
            WHERE id=%s AND status='ready'
            """,
            (row["lead_id"],),
        )
    else:
        conn.execute(
            "UPDATE outreach.messages SET status='failed',last_error=%s,updated_at=now() WHERE id=%s",
            (result.error, row["id"]),
        )
    conn.commit()
    return "sent" if result.accepted else "failed"


def process_one(conn, settings: Settings) -> bool:
    return process_one_result(conn, settings) is not None


def sent_today(conn, settings: Settings) -> int:
    row = conn.execute(
        """
        SELECT count(*) AS total
        FROM outreach.messages
        WHERE sent_at >= %s
           OR (status IN ('sending','delivery_uncertain') AND updated_at >= %s)
        """,
        (local_day_start(settings), local_day_start(settings)),
    ).fetchone()
    return row["total"]


def recover_stale_sending(conn, stale_minutes: int = 60) -> int:
    result = conn.execute(
        """
        UPDATE outreach.messages
        SET status='delivery_uncertain',
            last_error='Estado de entrega incerto após interrupção do worker',
            updated_at=now()
        WHERE status='sending'
          AND updated_at < now() - make_interval(mins => %s)
        """,
        (stale_minutes,),
    )
    conn.commit()
    return result.rowcount


def uncertain_delivery_count(conn) -> int:
    row = conn.execute(
        """
        SELECT count(*) AS total
        FROM outreach.messages
        WHERE status='delivery_uncertain'
        """
    ).fetchone()
    return row["total"]


def queued_for_campaign(conn, campaign_id: int) -> int:
    row = conn.execute(
        """
        SELECT count(*) AS total
        FROM outreach.messages
        WHERE campaign_id=%s
          AND status IN ('approved','queued')
          AND (scheduled_at IS NULL OR scheduled_at <= now())
        """,
        (campaign_id,),
    ).fetchone()
    return row["total"]


def process_batch(
    conn,
    settings: Settings,
    campaign_id: int,
    limit: int,
    interval_seconds: int,
) -> dict[str, int]:
    already_sent = sent_today(conn, settings)
    allowed = max(0, min(limit, settings.daily_limit - already_sent))
    summary = {"processed": 0, "sent": 0, "failed": 0}
    for index in range(allowed):
        status = process_one_result(conn, settings, campaign_id)
        if status is None:
            break
        summary["processed"] += 1
        summary[status] += 1
        if status == "failed":
            log.error("Lote interrompido após falha SMTP para evitar erros em sequência")
            break
        if interval_seconds > 0 and index + 1 < allowed:
            time.sleep(interval_seconds)
    summary["remaining_daily"] = max(0, settings.daily_limit - sent_today(conn, settings))
    summary["queued"] = queued_for_campaign(conn, campaign_id)
    return summary


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
