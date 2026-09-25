from datetime import datetime
from email.utils import parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Settings


STORY_TEMPLATE_NAMES = (
    "História 01 - Quando tudo depende de você",
    "História 02 - O oi das 23h47",
    "História 03 - O monstro da planilha",
    "História 04 - A bola pediu mais dados",
    "História 05 - A ferramenta que quase cabia",
    "História 06 - A aposentadoria do copiar e colar",
)
STORY_TEMPLATE_SEED_LOCK = 843_176_620_007


def ensure_story_templates(conn) -> int:
    placeholders = ",".join(["%s"] * len(STORY_TEMPLATE_NAMES))

    def active_count() -> int:
        row = conn.execute(
            f"""
            SELECT count(DISTINCT name) AS total
            FROM outreach.templates
            WHERE status='active' AND name IN ({placeholders})
            """,
            STORY_TEMPLATE_NAMES,
        ).fetchone()
        return int(row["total"])

    existing = active_count()
    if existing == len(STORY_TEMPLATE_NAMES):
        return 0

    conn.execute("SELECT pg_advisory_xact_lock(%s)", (STORY_TEMPLATE_SEED_LOCK,))
    existing = active_count()
    if existing == len(STORY_TEMPLATE_NAMES):
        return 0

    migration = (
        Path(__file__).resolve().parents[2]
        / "sql"
        / "007_story_email_templates.sql"
    )
    conn.execute(migration.read_text(encoding="utf-8"))
    return len(STORY_TEMPLATE_NAMES) - existing


def normalize_email(value: str | None) -> str | None:
    _, email = parseaddr(value or "")
    email = email.strip().lower()
    if not email or "@" not in email:
        return None
    local, domain = email.rsplit("@", 1)
    return email if local and "." in domain else None


def contact_role(email: str) -> str:
    local = email.split("@", 1)[0]
    if local in {"vendas", "comercial", "sales"}:
        return "sales"
    if local in {"contato", "atendimento", "relacionamento", "sac"}:
        return "support"
    if local in {"financeiro", "fiscal", "nfe", "contabilidade"}:
        return "finance"
    return "general"


def render(template: str, lead: dict) -> str:
    values = {
        "empresa": lead.get("trade_name") or lead.get("company_name") or "sua empresa",
        "razao_social": lead.get("company_name") or "",
        "cnpj": lead.get("cnpj") or "",
    }
    rendered = template
    for name, value in values.items():
        rendered = rendered.replace("{" + name + "}", str(value))
    return rendered


def sync_leads(conn, *, commit: bool = True) -> int:
    relation = conn.execute(
        "SELECT to_regclass('cnpj.prospectos_qualificados') AS prospects"
    ).fetchone()
    if not relation["prospects"]:
        raise RuntimeError("Tabela de prospects qualificados do CNPJ ETL não encontrada")
    result = conn.execute(
        r"""
        INSERT INTO outreach.leads
          (cnpj,company_name,trade_name,email,email_domain,phone,whatsapp,contact_role,
           lead_score,confidence_score,source_payload,source,status,updated_at)
        SELECT p.cnpj,p.razao_social,p.nome_fantasia,lower(btrim(p.email)),
          split_part(lower(btrim(p.email)),'@',2),p.telefone_1,p.whatsapp_url,
          CASE
            WHEN split_part(lower(btrim(p.email)),'@',1)
              IN ('vendas','comercial','sales') THEN 'sales'
            WHEN split_part(lower(btrim(p.email)),'@',1)
              IN ('contato','atendimento','relacionamento','sac') THEN 'support'
            WHEN split_part(lower(btrim(p.email)),'@',1)
              IN ('financeiro','fiscal','nfe','contabilidade') THEN 'finance'
            ELSE 'general'
          END,
          p.lead_score,p.confidence_score,
          jsonb_strip_nulls(jsonb_build_object(
            'lead_quality',p.lead_quality,
            'qualification_reasons',p.qualification_reasons,
            'qualification_version',p.qualification_version,
            'contact_channel',p.contact_channel,
            'marketing_ready',true,
            'marketing_ready_at',coalesce(p.qualified_at,now())
          )),'cnpj_etl','ready',now()
        FROM cnpj.prospectos_qualificados p
        WHERE p.qualification_status = 'qualified' AND p.lead_quality IN ('A', 'B')
          AND p.email IS NOT NULL
          AND btrim(p.email) ~* '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'
        ON CONFLICT (cnpj) DO UPDATE SET
          company_name=EXCLUDED.company_name,trade_name=EXCLUDED.trade_name,
          email=EXCLUDED.email,email_domain=EXCLUDED.email_domain,
          phone=EXCLUDED.phone,whatsapp=EXCLUDED.whatsapp,
          contact_role=EXCLUDED.contact_role,lead_score=EXCLUDED.lead_score,
          confidence_score=EXCLUDED.confidence_score,
          source_payload=EXCLUDED.source_payload,source=EXCLUDED.source,updated_at=now()
        WHERE (
          outreach.leads.company_name,outreach.leads.trade_name,outreach.leads.email,
          outreach.leads.phone,outreach.leads.whatsapp,outreach.leads.contact_role,
          outreach.leads.lead_score,outreach.leads.confidence_score,
          outreach.leads.source_payload
        ) IS DISTINCT FROM (
          EXCLUDED.company_name,EXCLUDED.trade_name,EXCLUDED.email,EXCLUDED.phone,
          EXCLUDED.whatsapp,EXCLUDED.contact_role,EXCLUDED.lead_score,
          EXCLUDED.confidence_score,EXCLUDED.source_payload
        )
        """
    )
    count = result.rowcount
    metrics_available = conn.execute(
        "SELECT to_regclass('outreach.lead_metrics') IS NOT NULL AS available"
    ).fetchone()["available"]
    if metrics_available:
        conn.execute(
            """
            INSERT INTO outreach.lead_metrics
              (singleton,total,quality_a,quality_b,with_whatsapp,public_profile,
               score_sum,updated_at)
            SELECT true,count(*) FILTER (WHERE email IS NOT NULL),
              count(*) FILTER (
                WHERE email IS NOT NULL AND source_payload->>'lead_quality'='A'
              ),
              count(*) FILTER (
                WHERE email IS NOT NULL AND source_payload->>'lead_quality'='B'
              ),
              count(*) FILTER (
                WHERE email IS NOT NULL AND NULLIF(whatsapp,'') IS NOT NULL
              ),
              count(*) FILTER (
                WHERE email IS NOT NULL
                  AND source_payload->'qualification_reasons'
                    ? 'perfil_publico_verificado'
              ),
              COALESCE(sum(COALESCE(lead_score,0)) FILTER (WHERE email IS NOT NULL),0),now()
            FROM outreach.leads
            ON CONFLICT (singleton) DO UPDATE SET
              total=EXCLUDED.total,quality_a=EXCLUDED.quality_a,
              quality_b=EXCLUDED.quality_b,with_whatsapp=EXCLUDED.with_whatsapp,
              public_profile=EXCLUDED.public_profile,score_sum=EXCLUDED.score_sum,
              updated_at=now()
            """
        )
    if commit:
        conn.commit()
    return count


def ensure_campaign(
    conn,
    campaign_key: str,
    name: str,
    subject_template: str,
    body_template: str,
    daily_limit: int,
    *,
    html_template: str | None = None,
    commit: bool = True,
):
    campaign = conn.execute(
        "SELECT * FROM outreach.campaigns WHERE campaign_key=%s",
        (campaign_key,),
    ).fetchone()
    if not campaign:
        campaign = conn.execute(
            """
            SELECT * FROM outreach.campaigns
            WHERE name=%s AND campaign_key IS NULL
            ORDER BY id LIMIT 1
            """,
            (name,),
        ).fetchone()
    if campaign:
        campaign = conn.execute(
            """
            UPDATE outreach.campaigns
            SET campaign_key=%s,name=%s,subject_template=%s,body_template=%s,
                body_html_template=%s,status='active',requires_approval=false,
                daily_limit=%s,updated_at=now()
            WHERE id=%s
            RETURNING *
            """,
            (
                campaign_key,
                name,
                subject_template,
                body_template,
                html_template,
                daily_limit,
                campaign["id"],
            ),
        ).fetchone()
    else:
        campaign = conn.execute(
            """
            INSERT INTO outreach.campaigns
              (campaign_key,name,subject_template,body_template,body_html_template,status,
               requires_approval,daily_limit)
            VALUES (%s,%s,%s,%s,%s,'active',false,%s)
            RETURNING *
            """,
            (campaign_key, name, subject_template, body_template, html_template, daily_limit),
        ).fetchone()
    if commit:
        conn.commit()
    return campaign


def prepare_campaign(
    conn,
    campaign_id: int,
    settings: Settings,
    *,
    commit: bool = True,
) -> int:
    campaign = conn.execute(
        "SELECT * FROM outreach.campaigns WHERE id=%s", (campaign_id,)
    ).fetchone()
    if not campaign or campaign["channel"] != "email":
        raise ValueError("Campanha de e-mail não encontrada")
    rows = conn.execute(
        """
        SELECT l.* FROM outreach.leads l
        WHERE l.status = 'ready' AND l.email IS NOT NULL
          AND l.contact_role NOT IN ('finance','accounting')
          AND NOT EXISTS (
            SELECT 1 FROM outreach.suppressions s
            WHERE s.channel='email' AND lower(s.destination)=lower(l.email)
          )
        ORDER BY l.lead_score DESC NULLS LAST, l.id
        """
    ).fetchall()
    initial_status = "pending_approval" if settings.require_approval else "queued"
    count = 0
    for lead in rows:
        result = conn.execute(
            """
            INSERT INTO outreach.messages
              (campaign_id,lead_id,channel,destination,destination_domain,subject,
               body_text,body_html,status,scheduled_at,sequence_step)
            VALUES (%s,%s,'email',%s,%s,%s,%s,%s,%s,now(),0)
            ON CONFLICT (campaign_id,lead_id,channel,sequence_step) DO NOTHING
            """,
            (
                campaign_id,
                lead["id"],
                lead["email"],
                lead["email_domain"],
                render(campaign["subject_template"] or "Contato Tironi Tech", lead),
                render(campaign["body_template"], lead),
                render(campaign["body_html_template"], lead)
                if campaign.get("body_html_template")
                else None,
                initial_status,
            ),
        )
        count += result.rowcount
    if commit:
        conn.commit()
    return count


def prepare_followups(
    conn,
    campaign_id: int,
    subject_template: str,
    body_template: str,
    delay_days: int,
    *,
    commit: bool = True,
) -> int:
    rows = conn.execute(
        """
        SELECT l.* FROM outreach.leads l
        JOIN outreach.messages initial
          ON initial.lead_id=l.id
         AND initial.campaign_id=%s
         AND initial.channel='email'
         AND initial.sequence_step=0
        WHERE l.status='contacted'
          AND initial.status IN ('sent','delivered')
          AND initial.sent_at <= now() - make_interval(days => %s)
          AND NOT EXISTS (
            SELECT 1 FROM outreach.messages followup
            WHERE followup.campaign_id=initial.campaign_id
              AND followup.lead_id=initial.lead_id
              AND followup.channel=initial.channel
              AND followup.sequence_step=1
          )
          AND NOT EXISTS (
            SELECT 1 FROM outreach.suppressions s
            WHERE s.channel='email' AND lower(s.destination)=lower(l.email)
          )
        ORDER BY initial.sent_at, l.id
        """,
        (campaign_id, delay_days),
    ).fetchall()
    count = 0
    for lead in rows:
        result = conn.execute(
            """
            INSERT INTO outreach.messages
              (campaign_id,lead_id,channel,destination,destination_domain,subject,
               body_text,status,scheduled_at,sequence_step)
            VALUES (%s,%s,'email',%s,%s,%s,%s,'queued',now(),1)
            ON CONFLICT (campaign_id,lead_id,channel,sequence_step) DO NOTHING
            """,
            (
                campaign_id,
                lead["id"],
                lead["email"],
                lead["email_domain"],
                render(subject_template, lead),
                render(body_template, lead),
            ),
        )
        count += result.rowcount
    if commit:
        conn.commit()
    return count


def inside_send_window(settings: Settings) -> bool:
    hour = datetime.now(ZoneInfo(settings.timezone)).hour
    return settings.send_start_hour <= hour < settings.send_end_hour
