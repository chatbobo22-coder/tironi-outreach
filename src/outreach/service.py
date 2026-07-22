from datetime import datetime
from email.utils import parseaddr
import json
from zoneinfo import ZoneInfo

from .config import Settings


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
    return template.format_map(values)


def sync_leads(conn, *, commit: bool = True) -> int:
    relations = conn.execute(
        "SELECT to_regclass('cnpj.v_prospectos_outreach_v2') AS v2, "
        "to_regclass('cnpj.v_prospectos_outreach') AS v1"
    ).fetchone()
    if relations["v2"]:
        query = """
        SELECT cnpj, razao_social, nome_fantasia, email, telefone_1,
               lead_score, confidence_score, to_jsonb(v) AS payload
        FROM cnpj.v_prospectos_outreach_v2 v
        WHERE qualification_status = 'qualified'
        """
    elif relations["v1"]:
        query = """
        SELECT cnpj, razao_social, nome_fantasia, email, telefone_1,
               digital_score AS lead_score, NULL::smallint AS confidence_score,
               to_jsonb(v) AS payload
        FROM cnpj.v_prospectos_outreach v
        """
    else:
        raise RuntimeError("View de prospects do CNPJ ETL não encontrada")
    count = 0
    for row in conn.execute(query).fetchall():
        email = normalize_email(row["email"])
        if not email:
            continue
        domain = email.rsplit("@", 1)[1]
        result = conn.execute(
            """
            INSERT INTO outreach.leads
              (cnpj,company_name,trade_name,email,email_domain,phone,contact_role,
               lead_score,confidence_score,source_payload,status,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ready',now())
            ON CONFLICT (cnpj) DO UPDATE SET
              company_name=EXCLUDED.company_name, trade_name=EXCLUDED.trade_name,
              email=EXCLUDED.email, email_domain=EXCLUDED.email_domain,
              phone=EXCLUDED.phone, contact_role=EXCLUDED.contact_role,
              lead_score=EXCLUDED.lead_score, confidence_score=EXCLUDED.confidence_score,
              source_payload=EXCLUDED.source_payload, updated_at=now()
            """,
            (
                row["cnpj"],
                row["razao_social"],
                row["nome_fantasia"],
                email,
                domain,
                row["telefone_1"],
                contact_role(email),
                row["lead_score"],
                row["confidence_score"],
                json.dumps(row["payload"], default=str),
            ),
        )
        count += result.rowcount
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
            SET campaign_key=%s,name=%s,subject_template=%s,body_template=%s,status='active',
                requires_approval=false,daily_limit=%s,updated_at=now()
            WHERE id=%s
            RETURNING *
            """,
            (
                campaign_key,
                name,
                subject_template,
                body_template,
                daily_limit,
                campaign["id"],
            ),
        ).fetchone()
    else:
        campaign = conn.execute(
            """
            INSERT INTO outreach.campaigns
              (campaign_key,name,subject_template,body_template,status,
               requires_approval,daily_limit)
            VALUES (%s,%s,%s,%s,'active',false,%s)
            RETURNING *
            """,
            (campaign_key, name, subject_template, body_template, daily_limit),
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
               body_text,status,scheduled_at,sequence_step)
            VALUES (%s,%s,'email',%s,%s,%s,%s,%s,now(),0)
            ON CONFLICT (campaign_id,lead_id,channel,sequence_step) DO NOTHING
            """,
            (
                campaign_id,
                lead["id"],
                lead["email"],
                lead["email_domain"],
                render(campaign["subject_template"] or "Contato Tironi Tech", lead),
                render(campaign["body_template"], lead),
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
