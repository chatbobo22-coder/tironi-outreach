from pathlib import Path

from outreach.service import STORY_TEMPLATE_NAMES, ensure_story_templates


MIGRATION = (
    Path(__file__).resolve().parents[1] / "sql" / "007_story_email_templates.sql"
)


def test_story_template_seed_is_complete_and_idempotent():
    sql = MIGRATION.read_text(encoding="utf-8")

    assert sql.count("'História 0") == 6
    assert sql.count("https://www.tironitech.com/email/historias/") == 6
    assert sql.count("https://wa.me/5543996676633?") == 12
    assert "{empresa}" in sql
    assert "{unsubscribe_url}" in sql
    assert "WHERE NOT EXISTS" in sql


def test_story_template_html_has_email_fallbacks_and_ctas():
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "@media only screen and (max-width:620px)" in sql
    assert 'alt="%2$s"' in sql
    assert "Quero encontrar meu primeiro gargalo" in sql
    assert "Quero organizar meu atendimento" in sql
    assert "Quero domar minhas planilhas" in sql
    assert "Quero enxergar melhor minha operação" in sql
    assert "Quero conversar sobre meu sistema" in sql
    assert "Quero aposentar o copiar e colar" in sql


class StoryTemplateSeedConnection:
    def __init__(self, counts):
        self.counts = iter(counts)
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))
        if "count(DISTINCT name)" in query:
            return StoryTemplateSeedResult({"total": next(self.counts)})
        return StoryTemplateSeedResult(None)


class StoryTemplateSeedResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


def test_story_template_seed_skips_database_when_all_models_exist():
    conn = StoryTemplateSeedConnection([len(STORY_TEMPLATE_NAMES)])

    assert ensure_story_templates(conn) == 0
    assert len(conn.queries) == 1


def test_story_template_seed_is_locked_and_applied_when_models_are_missing():
    conn = StoryTemplateSeedConnection([0, 0])

    assert ensure_story_templates(conn) == len(STORY_TEMPLATE_NAMES)
    assert "pg_advisory_xact_lock" in conn.queries[1][0]
    assert "INSERT INTO outreach.templates" in conn.queries[3][0]
