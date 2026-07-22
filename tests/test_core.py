from types import SimpleNamespace

from outreach import cli
from outreach import worker
from outreach.campaign import (
    BODY_TEMPLATE,
    FOLLOW_UP_BODY_TEMPLATE,
    FOLLOW_UP_SUBJECT_TEMPLATE,
    SUBJECT_TEMPLATE,
)
from outreach.security import unsubscribe_token, valid_unsubscribe_token
from outreach.service import (
    contact_role,
    normalize_email,
    prepare_campaign,
    prepare_followups,
    render,
)


def test_email_normalization():
    assert normalize_email("Contato <Contato@Empresa.com.br>") == "contato@empresa.com.br"
    assert normalize_email("inválido") is None


def test_contact_roles():
    assert contact_role("vendas@empresa.com.br") == "sales"
    assert contact_role("fiscal@empresa.com.br") == "finance"


def test_render():
    assert render("Olá, {empresa}", {"trade_name": "Loja X"}) == "Olá, Loja X"
    assert "Loja X" in render(SUBJECT_TEMPLATE, {"trade_name": "Loja X"})
    assert "Olá, equipe da Loja X" in render(BODY_TEMPLATE, {"trade_name": "Loja X"})


def test_unsubscribe_signature():
    token = unsubscribe_token(42, "a@b.com", "secret")
    assert valid_unsubscribe_token(42, "a@b.com", token, "secret")
    assert not valid_unsubscribe_token(43, "a@b.com", token, "secret")


class FakeResult:
    def __init__(self, *, row=None, rows=None, rowcount=0):
        self.row = row
        self.rows = rows or []
        self.rowcount = rowcount

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class PrepareCampaignConnection:
    def __init__(self):
        self.insert_count = 0

    def execute(self, query, params=None):
        if "SELECT * FROM outreach.campaigns" in query:
            return FakeResult(
                row={
                    "id": 7,
                    "channel": "email",
                    "subject_template": SUBJECT_TEMPLATE,
                    "body_template": BODY_TEMPLATE,
                }
            )
        if "SELECT l.* FROM outreach.leads" in query:
            return FakeResult(
                rows=[
                    {
                        "id": 11,
                        "email": "contato@empresa.com.br",
                        "email_domain": "empresa.com.br",
                        "company_name": "Empresa",
                        "trade_name": "Loja X",
                    }
                ]
            )
        if "INSERT INTO outreach.messages" in query:
            self.insert_count += 1
            return FakeResult(rowcount=1 if self.insert_count == 1 else 0)
        raise AssertionError(query)

    def commit(self):
        pass


def test_prepare_campaign_counts_only_new_messages():
    conn = PrepareCampaignConnection()
    settings = SimpleNamespace(require_approval=False)

    assert prepare_campaign(conn, 7, settings) == 1
    assert prepare_campaign(conn, 7, settings) == 0


class PrepareFollowupConnection:
    def __init__(self):
        self.insert_count = 0
        self.selection_params = None
        self.selection_query = None

    def execute(self, query, params=None):
        if "JOIN outreach.messages initial" in query:
            self.selection_query = query
            self.selection_params = params
            return FakeResult(
                rows=[
                    {
                        "id": 11,
                        "email": "contato@empresa.com.br",
                        "email_domain": "empresa.com.br",
                        "company_name": "Empresa",
                        "trade_name": "Loja X",
                    }
                ]
            )
        if "INSERT INTO outreach.messages" in query:
            self.insert_count += 1
            return FakeResult(rowcount=1 if self.insert_count == 1 else 0)
        raise AssertionError(query)

    def commit(self):
        pass


def test_followup_uses_delay_and_is_not_duplicated():
    conn = PrepareFollowupConnection()

    first = prepare_followups(
        conn,
        7,
        FOLLOW_UP_SUBJECT_TEMPLATE,
        FOLLOW_UP_BODY_TEMPLATE,
        delay_days=7,
    )
    second = prepare_followups(
        conn,
        7,
        FOLLOW_UP_SUBJECT_TEMPLATE,
        FOLLOW_UP_BODY_TEMPLATE,
        delay_days=7,
    )

    assert first == 1
    assert second == 0
    assert conn.selection_params == (7, 7)
    assert "l.status='contacted'" in conn.selection_query


def test_batch_respects_limit_and_stops_after_failure(monkeypatch):
    sent_totals = iter([10, 11])
    statuses = iter(["sent", "failed", "sent"])
    monkeypatch.setattr(worker, "sent_today", lambda conn, settings: next(sent_totals))
    monkeypatch.setattr(
        worker,
        "process_one_result",
        lambda conn, settings, campaign_id: next(statuses),
    )
    monkeypatch.setattr(worker, "queued_for_campaign", lambda conn, campaign_id: 20)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
    settings = SimpleNamespace(daily_limit=300)

    result = worker.process_batch(object(), settings, 7, limit=300, interval_seconds=30)

    assert result["processed"] == 2
    assert result["sent"] == 1
    assert result["failed"] == 1
    assert result["remaining_daily"] == 289


def test_batch_never_exceeds_remaining_daily_limit(monkeypatch):
    sent_totals = iter([299, 300])
    monkeypatch.setattr(worker, "sent_today", lambda conn, settings: next(sent_totals))
    monkeypatch.setattr(
        worker,
        "process_one_result",
        lambda conn, settings, campaign_id: "sent",
    )
    monkeypatch.setattr(worker, "queued_for_campaign", lambda conn, campaign_id: 20)
    settings = SimpleNamespace(daily_limit=300)

    result = worker.process_batch(object(), settings, 7, limit=300, interval_seconds=0)

    assert result["processed"] == 1
    assert result["remaining_daily"] == 0


class DryRunDatabase:
    class Connection:
        def __init__(self):
            self.rolled_back = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def rollback(self):
            self.rolled_back = True

    def __init__(self):
        self.connection = self.Connection()

    def connect(self):
        return self.connection


def test_daily_dry_run_does_not_sync_or_prepare(monkeypatch):
    monkeypatch.setattr(
        cli,
        "sync_leads",
        lambda conn, commit: 12 if commit is False else None,
    )
    monkeypatch.setattr(
        cli,
        "ensure_campaign",
        lambda *args, commit: {"id": 7} if commit is False else None,
    )
    monkeypatch.setattr(
        cli,
        "prepare_campaign",
        lambda conn, campaign_id, settings, commit: 10 if commit is False else None,
    )
    monkeypatch.setattr(
        cli,
        "prepare_followups",
        lambda *args, commit: 2 if commit is False else None,
    )
    monkeypatch.setattr(cli, "queued_for_campaign", lambda conn, campaign_id: 15)
    monkeypatch.setattr(cli, "sent_today", lambda conn, settings: 5)
    settings = SimpleNamespace(
        dry_run=True,
        daily_limit=300,
        followup_delay_days=7,
        max_followups=1,
    )
    db = DryRunDatabase()

    result = cli.run_daily(settings, db, limit=1, interval_seconds=0)

    assert result["would_send"] == 1
    assert result["processed"] == 0
    assert result["synced"] == 12
    assert db.connection.rolled_back


def test_daily_run_blocks_on_uncertain_delivery(monkeypatch):
    monkeypatch.setattr(cli, "recover_stale_sending", lambda conn: 1)
    monkeypatch.setattr(cli, "uncertain_delivery_count", lambda conn: 1)
    monkeypatch.setattr(cli, "sent_today", lambda conn, settings: 1)
    monkeypatch.setattr(
        cli,
        "sync_leads",
        lambda conn: (_ for _ in ()).throw(
            AssertionError("não deve sincronizar com entrega incerta")
        ),
    )
    settings = SimpleNamespace(dry_run=False, daily_limit=300)

    result = cli.run_daily(settings, DryRunDatabase(), limit=300, interval_seconds=30)

    assert result["delivery_uncertain"] == 1
    assert result["processed"] == 0
