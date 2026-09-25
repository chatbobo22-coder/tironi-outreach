from pathlib import Path

from outreach.service import audience_filter, ensure_dispatch_schema


def test_quality_audience_filter_uses_selected_groups():
    clause, params = audience_filter(
        {"audience_mode": "quality", "audience_qualities": ["A"]}
    )

    assert "lead_quality" in clause
    assert params == ("A",)


def test_score_audience_filter_uses_inclusive_range():
    clause, params = audience_filter(
        {"audience_mode": "score", "min_score": 70, "max_score": 95}
    )

    assert "BETWEEN %s AND %s" in clause
    assert params == (70, 95)


class DispatchSchemaResult:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class DispatchSchemaConnection:
    def __init__(self, availability):
        self.availability = iter(availability)
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))
        if "information_schema.columns" in query:
            return DispatchSchemaResult({"available": next(self.availability)})
        return DispatchSchemaResult()


def test_dispatch_schema_is_applied_once_when_missing():
    conn = DispatchSchemaConnection([False, False])

    assert ensure_dispatch_schema(conn) is True
    assert "pg_advisory_xact_lock" in conn.queries[1][0]
    assert "ADD COLUMN IF NOT EXISTS audience_mode" in conn.queries[3][0]


def test_dispatch_migration_contains_campaign_controls():
    sql = (
        Path(__file__).resolve().parents[1] / "sql" / "008_campaign_audiences.sql"
    ).read_text(encoding="utf-8")

    assert "audience_qualities" in sql
    assert "scheduled_start_at" in sql
    assert "launched_at" in sql
