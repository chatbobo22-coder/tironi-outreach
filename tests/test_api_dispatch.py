from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from outreach import api


class ConnectionContext:
    def __enter__(self):
        return "connection"

    def __exit__(self, *_args):
        return False


class FakeDatabase:
    def connect(self):
        return ConnectionContext()


def dispatch_settings(**overrides):
    values = {
        "dry_run": False,
        "cron_secret": "secret-value",
        "cron_secret_sha256": "",
        "hourly_limit": 50,
        "validate_smtp": lambda: None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_hourly_dispatch_requires_dedicated_secret(monkeypatch):
    monkeypatch.setattr(api, "settings", dispatch_settings())

    with pytest.raises(HTTPException) as exc:
        api.dispatch_hourly("wrong")

    assert exc.value.status_code == 401


def test_hourly_dispatch_accepts_hashed_secret(monkeypatch):
    monkeypatch.setattr(
        api,
        "settings",
        dispatch_settings(
            cron_secret="different-environment-secret",
            cron_secret_sha256=(
                "b647d0ec3a15215ba8f8a9e5e4474dcfcf363f11a9e8a4278060ea55b103f3eb"
            ),
        ),
    )
    monkeypatch.setattr(api, "db", FakeDatabase())
    monkeypatch.setattr(
        api,
        "process_batch",
        lambda *_args: {"processed": 0, "sent": 0, "failed": 0},
    )

    result = api.dispatch_hourly("github-cron-secret")

    assert result["processed"] == 0


def test_hourly_dispatch_processes_up_to_hourly_limit(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "settings", dispatch_settings())
    monkeypatch.setattr(api, "db", FakeDatabase())
    monkeypatch.setattr(
        api,
        "process_batch",
        lambda conn, settings, campaign_id, limit, interval: calls.append(
            (conn, campaign_id, limit, interval)
        )
        or {"processed": 12, "sent": 12, "failed": 0},
    )

    result = api.dispatch_hourly("secret-value")

    assert result["sent"] == 12
    assert calls == [("connection", None, 50, 0)]


def test_manual_batch_targets_selected_campaign(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "settings", dispatch_settings())
    monkeypatch.setattr(api, "db", FakeDatabase())
    monkeypatch.setattr(
        api,
        "process_batch",
        lambda conn, settings, campaign_id, limit, interval: calls.append(
            (campaign_id, limit, interval)
        )
        or {"processed": 20, "sent": 20, "failed": 0},
    )

    result = api.send_campaign_batch(7, 50)

    assert result["processed"] == 20
    assert calls == [(7, 50, 0)]
