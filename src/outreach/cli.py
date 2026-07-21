import argparse
from pathlib import Path

from .config import Settings
from .db import Database
from .service import prepare_campaign, sync_leads


def main():
    parser = argparse.ArgumentParser(description="Tironi Outreach")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    sub.add_parser("sync-leads")
    prepare = sub.add_parser("prepare-campaign")
    prepare.add_argument("campaign_id", type=int)
    approve = sub.add_parser("approve-campaign")
    approve.add_argument("campaign_id", type=int)
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


if __name__ == "__main__":
    main()
