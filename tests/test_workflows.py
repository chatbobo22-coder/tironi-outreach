from pathlib import Path


def test_hourly_schedule_retries_off_peak_and_keeps_database_quota_guard():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/daily-outreach.yml").read_text(
        encoding="utf-8"
    )
    worker = (root / "src/outreach/worker.py").read_text(encoding="utf-8")

    assert 'cron: "7,22,37,52 12-19 * * 1-5"' in workflow
    assert "HOURLY_EMAIL_LIMIT: \"50\"" in workflow
    assert "now() - interval '1 hour'" in worker
    assert "settings.hourly_limit" in worker
