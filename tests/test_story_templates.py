from pathlib import Path


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
