import argparse
import json
from pathlib import Path

from .campaign import (
    BODY_TEMPLATE,
    CAMPAIGN_KEY,
    CAMPAIGN_NAME,
    FOLLOW_UP_BODY_TEMPLATE,
    FOLLOW_UP_SUBJECT_TEMPLATE,
    SUBJECT_TEMPLATE,
)
from .config import Settings
from .db import Database
from .service import (
    ensure_campaign,
    prepare_campaign,
    prepare_followups,
    sync_leads,
)
from .worker import (
    process_batch,
    queued_for_campaign,
    recover_stale_sending,
    sent_today,
    uncertain_delivery_count,
)


def run_daily(settings: Settings, db: Database, limit: int, interval_seconds: int) -> dict:
    with db.connect() as conn:
        if settings.dry_run:
            synced = sync_leads(conn, commit=False)
            campaign = ensure_campaign(
                conn,
                CAMPAIGN_KEY,
                CAMPAIGN_NAME,
                SUBJECT_TEMPLATE,
                BODY_TEMPLATE,
                settings.daily_limit,
                commit=False,
            )
            prepared_initial = prepare_campaign(
                conn,
                campaign["id"],
                settings,
                commit=False,
            )
            prepared_followups = 0
            if settings.max_followups > 0:
                prepared_followups = prepare_followups(
                    conn,
                    campaign["id"],
                    FOLLOW_UP_SUBJECT_TEMPLATE,
                    FOLLOW_UP_BODY_TEMPLATE,
                    settings.followup_delay_days,
                    commit=False,
                )
            queued = queued_for_campaign(conn, campaign["id"])
            remaining = max(0, settings.daily_limit - sent_today(conn, settings))
            conn.rollback()
            return {
                "campaign_id": campaign["id"],
                "synced": synced,
                "prepared_initial": prepared_initial,
                "prepared_followups": prepared_followups,
                "processed": 0,
                "sent": 0,
                "failed": 0,
                "remaining_daily": remaining,
                "queued": queued,
                "would_send": min(limit, remaining, queued),
                "dry_run": True,
            }

        recovered_stale = recover_stale_sending(conn)
        uncertain = uncertain_delivery_count(conn)
        if uncertain:
            return {
                "campaign_id": None,
                "synced": 0,
                "prepared_initial": 0,
                "prepared_followups": 0,
                "processed": 0,
                "sent": 0,
                "failed": 0,
                "delivery_uncertain": uncertain,
                "recovered_stale": recovered_stale,
                "remaining_daily": max(0, settings.daily_limit - sent_today(conn, settings)),
                "queued": 0,
                "dry_run": False,
            }
        synced = sync_leads(conn)
        campaign = ensure_campaign(
            conn,
            CAMPAIGN_KEY,
            CAMPAIGN_NAME,
            SUBJECT_TEMPLATE,
            BODY_TEMPLATE,
            settings.daily_limit,
        )
        prepared_initial = prepare_campaign(conn, campaign["id"], settings)
        prepared_followups = 0
        if settings.max_followups > 0:
            prepared_followups = prepare_followups(
                conn,
                campaign["id"],
                FOLLOW_UP_SUBJECT_TEMPLATE,
                FOLLOW_UP_BODY_TEMPLATE,
                settings.followup_delay_days,
            )

        settings.validate_smtp()
        delivery = process_batch(
            conn,
            settings,
            campaign["id"],
            limit,
            interval_seconds,
        )
    return {
        "campaign_id": campaign["id"],
        "synced": synced,
        "prepared_initial": prepared_initial,
        "prepared_followups": prepared_followups,
        "recovered_stale": recovered_stale,
        "dry_run": False,
        **delivery,
    }


def main():
    parser = argparse.ArgumentParser(description="Tironi Outreach")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    sub.add_parser("sync-leads")
    prepare = sub.add_parser("prepare-campaign")
    prepare.add_argument("campaign_id", type=int)
    approve = sub.add_parser("approve-campaign")
    approve.add_argument("campaign_id", type=int)
    daily = sub.add_parser("run-daily")
    daily.add_argument("--limit", type=int, default=None)
    daily.add_argument("--interval", type=int, default=None)
    args = parser.parse_args()
    settings, db = Settings(), Database(Settings().database_url)
    sql_dir = Path(__file__).resolve().parents[2] / "sql"
    db.migrate(sql_dir)
    if args.command == "sync-leads":
        with db.connect() as conn:
            print({"synced": sync_leads(conn)})
    elif args.command == "prepare-campaign":
        with db.connect() as conn:
            print({"prepared": prepare_campaign(conn, args.campaign_id, settings)})
    elif args.command == "approve-campaign":
        with db.connect() as conn:
            result = conn.execute(
                "UPDATE outreach.messages SET status='queued',approved_at=now(),updated_at=now() WHERE campaign_id=%s AND status='pending_approval'",
                (args.campaign_id,),
            )
            conn.commit()
            print({"approved": result.rowcount})
    elif args.command == "run-daily":
        limit = settings.daily_limit if args.limit is None else args.limit
        interval = settings.send_interval_seconds if args.interval is None else args.interval
        if limit < 1:
            parser.error("--limit precisa ser maior que zero")
        if interval < 0:
            parser.error("--interval não pode ser negativo")
        result = run_daily(settings, db, limit, interval)
        print(json.dumps(result, ensure_ascii=False))
        if result["failed"] or result.get("delivery_uncertain"):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
