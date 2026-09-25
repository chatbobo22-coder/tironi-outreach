import json
from datetime import datetime
from typing import Literal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .config import Settings
from .db import Database
from .providers.base import OutboundEmail
from .providers.smtp import SmtpProvider
from .reputation import (
    ReputationSnapshot,
    normalize_sendpulse_event,
    permanent_suppression_reason,
    reputation_pause_reasons,
)
from .security import valid_unsubscribe_token
from .service import (
    audience_count,
    ensure_dispatch_schema,
    ensure_story_templates,
    normalize_email,
    prepare_campaign,
    render,
    sync_leads,
)
from .worker import process_batch, process_one_result

app = FastAPI(title="Tironi Outreach", version="1.0.0")
settings = Settings()
db = Database(settings.database_url)


def auth(x_api_key: str | None = Header(default=None)):
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(401, "Não autorizado")


class CampaignIn(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    subject_template: str = Field(min_length=3, max_length=200)
    body_template: str = Field(min_length=10, max_length=10000)
    body_html_template: str | None = Field(default=None, max_length=100000)
    template_id: int | None = None
    daily_limit: int = Field(default=400, ge=1, le=1000)
    audience_mode: Literal["all", "quality", "score"] = "all"
    audience_qualities: list[Literal["A", "B"]] = Field(default_factory=lambda: ["A", "B"])
    min_score: int | None = Field(default=None, ge=0, le=100)
    max_score: int | None = Field(default=None, ge=0, le=100)
    scheduled_at: datetime | None = None
    launch: bool = False


class TemplateIn(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    subject: str = Field(min_length=3, max_length=200)
    preheader: str = Field(default="", max_length=240)
    text_body: str = Field(min_length=10, max_length=10000)
    html_body: str = Field(min_length=10, max_length=100000)


class DeliveryResolutionIn(BaseModel):
    delivered: bool


class AudienceEstimateIn(BaseModel):
    audience_mode: Literal["all", "quality", "score"] = "all"
    audience_qualities: list[Literal["A", "B"]] = Field(default_factory=lambda: ["A", "B"])
    min_score: int | None = Field(default=None, ge=0, le=100)
    max_score: int | None = Field(default=None, ge=0, le=100)


class TestEmailIn(BaseModel):
    template_id: int
    email: str = Field(min_length=5, max_length=320)
    company_name: str = Field(default="Empresa de teste", min_length=2, max_length=160)


CrmServiceType = Literal[
    "prospecting",
    "qualification",
    "demo",
    "proposal",
    "negotiation",
    "follow_up",
    "reactivation",
    "after_sales",
]
CrmStage = Literal[
    "ready",
    "contacted",
    "replied",
    "qualified",
    "meeting",
    "proposal",
    "won",
    "lost",
]


class CrmCaseIn(BaseModel):
    lead_id: int
    service_type: CrmServiceType = "prospecting"
    owner_name: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=10000)
    next_action_at: str | None = None


class CrmCaseUpdate(BaseModel):
    service_type: CrmServiceType | None = None
    stage: CrmStage | None = None
    owner_name: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=10000)
    next_action_at: str | None = None


@app.exception_handler(psycopg.Error)
def database_error(_request: Request, _exc: psycopg.Error):
    return JSONResponse(
        status_code=503,
        content={"detail": "Banco de dados indisponível"},
    )


@app.get("/health")
def health():
    return {"status": "ok", "dry_run": settings.dry_run}


@app.get("/health/database", dependencies=[Depends(auth)])
def database_health():
    with db.connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/api/leads/sync", dependencies=[Depends(auth)])
def api_sync_leads():
    with db.connect() as conn:
        return {"synced": sync_leads(conn)}


@app.post("/api/campaigns", dependencies=[Depends(auth)])
def create_campaign(data: CampaignIn):
    if data.min_score is not None and data.max_score is not None:
        if data.min_score > data.max_score:
            raise HTTPException(422, "Score mínimo não pode superar o máximo")
    with db.connect() as conn:
        ensure_dispatch_schema(conn)
        row = conn.execute(
            "INSERT INTO outreach.campaigns "
            "(name,subject_template,body_template,body_html_template,template_id,"
            "daily_limit,requires_approval,audience_mode,audience_qualities,min_score,"
            "max_score,scheduled_start_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                data.name,
                data.subject_template,
                data.body_template,
                data.body_html_template,
                data.template_id,
                data.daily_limit,
                settings.require_approval,
                data.audience_mode,
                data.audience_qualities,
                data.min_score,
                data.max_score,
                data.scheduled_at,
            ),
        ).fetchone()
        audience = audience_count(conn, row)
        launch_result = _launch_campaign(conn, row["id"]) if data.launch else None
        conn.commit()
    return {
        "id": row["id"],
        "audience": audience,
        "status": "active" if launch_result else "draft",
        "queued": launch_result["queued"] if launch_result else 0,
    }


@app.post("/api/audience/estimate", dependencies=[Depends(auth)])
def estimate_audience(data: AudienceEstimateIn):
    if data.min_score is not None and data.max_score is not None:
        if data.min_score > data.max_score:
            raise HTTPException(422, "Score mínimo não pode superar o máximo")
    with db.connect() as conn:
        ensure_dispatch_schema(conn)
        return {
            "audience": audience_count(
                conn,
                {
                    "audience_mode": data.audience_mode,
                    "audience_qualities": data.audience_qualities,
                    "min_score": data.min_score,
                    "max_score": data.max_score,
                },
            )
        }


@app.post("/api/test-email", dependencies=[Depends(auth)])
def send_test_email(data: TestEmailIn):
    destination = normalize_email(data.email)
    if not destination:
        raise HTTPException(422, "Informe um e-mail válido")
    try:
        settings.validate_smtp()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    with db.connect() as conn:
        template = conn.execute(
            "SELECT * FROM outreach.templates WHERE id=%s AND status='active'",
            (data.template_id,),
        ).fetchone()
    if not template:
        raise HTTPException(404, "Modelo não encontrado")
    lead = {"trade_name": data.company_name, "company_name": data.company_name}
    result = SmtpProvider(settings).send(
        OutboundEmail(
            message_id=0,
            to=destination,
            subject=f"[TESTE] {render(template['subject'], lead)}",
            text=render(template["text_body"], lead),
            html=render(template["html_body"], lead),
            unsubscribe_url="https://www.tironitech.com/",
        )
    )
    if not result.accepted:
        raise HTTPException(502, result.error or "Provedor recusou o teste")
    return {"sent": True, "destination": destination}


@app.get("/api/templates", dependencies=[Depends(auth)])
def list_templates():
    with db.connect() as conn:
        ensure_story_templates(conn)
        return {
            "templates": conn.execute(
                """
                SELECT t.*,
                  count(m.id) FILTER (WHERE m.status IN ('sent','delivered')) AS sent_count,
                  count(m.id) FILTER (WHERE EXISTS (
                    SELECT 1 FROM outreach.events e
                    WHERE e.message_id=m.id AND e.event_type IN ('open','opened')
                  )) AS opened_count,
                  count(m.id) FILTER (WHERE EXISTS (
                    SELECT 1 FROM outreach.events e
                    WHERE e.message_id=m.id AND e.event_type IN ('click','clicked')
                  )) AS clicked_count,
                  count(m.id) FILTER (WHERE m.status='replied') AS replied_count
                FROM outreach.templates t
                LEFT JOIN outreach.campaigns c ON c.template_id=t.id
                LEFT JOIN outreach.messages m ON m.campaign_id=c.id
                WHERE t.status='active'
                GROUP BY t.id
                ORDER BY t.updated_at DESC
                """
            ).fetchall()
        }


@app.post("/api/templates", dependencies=[Depends(auth)])
def create_template(data: TemplateIn):
    with db.connect() as conn:
        row = conn.execute(
            """
            INSERT INTO outreach.templates (name,subject,preheader,text_body,html_body)
            VALUES (%s,%s,%s,%s,%s) RETURNING *
            """,
            (data.name, data.subject, data.preheader, data.text_body, data.html_body),
        ).fetchone()
        conn.commit()
    return {"template": row}


@app.put("/api/templates/{template_id}", dependencies=[Depends(auth)])
def update_template(template_id: int, data: TemplateIn):
    with db.connect() as conn:
        row = conn.execute(
            """
            UPDATE outreach.templates
            SET name=%s,subject=%s,preheader=%s,text_body=%s,html_body=%s,updated_at=now()
            WHERE id=%s AND status='active' RETURNING *
            """,
            (
                data.name,
                data.subject,
                data.preheader,
                data.text_body,
                data.html_body,
                template_id,
            ),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Modelo não encontrado")
        conn.commit()
    return {"template": row}


@app.delete("/api/templates/{template_id}", dependencies=[Depends(auth)])
def delete_template(template_id: int):
    with db.connect() as conn:
        in_use = conn.execute(
            "SELECT count(*) AS total FROM outreach.campaigns WHERE template_id=%s",
            (template_id,),
        ).fetchone()
        if in_use["total"]:
            raise HTTPException(409, "Modelo utilizado por uma campanha")
        result = conn.execute(
            "UPDATE outreach.templates SET status='archived',updated_at=now() WHERE id=%s AND status='active'",
            (template_id,),
        )
        if not result.rowcount:
            raise HTTPException(404, "Modelo não encontrado")
        conn.commit()
    return {"deleted": True}


@app.post("/api/campaigns/{campaign_id}/prepare", dependencies=[Depends(auth)])
def api_prepare(campaign_id: int):
    with db.connect() as conn:
        try:
            count = prepare_campaign(conn, campaign_id, settings)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
    return {"prepared": count}


@app.post("/api/campaigns/{campaign_id}/launch", dependencies=[Depends(auth)])
def launch_campaign(campaign_id: int):
    with db.connect() as conn:
        ensure_dispatch_schema(conn)
        result = _launch_campaign(conn, campaign_id)
        conn.commit()
    return result


def _launch_campaign(conn, campaign_id: int) -> dict[str, int]:
    campaign = conn.execute(
        "SELECT * FROM outreach.campaigns WHERE id=%s", (campaign_id,)
    ).fetchone()
    if not campaign:
        raise HTTPException(404, "Campanha não encontrada")
    prepared = prepare_campaign(conn, campaign_id, settings, commit=False)
    approved = conn.execute(
        """
        UPDATE outreach.messages
        SET status='queued',approved_at=now(),updated_at=now()
        WHERE campaign_id=%s AND status='pending_approval'
        """,
        (campaign_id,),
    ).rowcount
    conn.execute(
        """
        UPDATE outreach.campaigns
        SET status='active',launched_at=coalesce(launched_at,now()),updated_at=now()
        WHERE id=%s
        """,
        (campaign_id,),
    )
    queued = conn.execute(
        """
        SELECT count(*) AS total FROM outreach.messages
        WHERE campaign_id=%s AND status IN ('approved','queued')
        """,
        (campaign_id,),
    ).fetchone()["total"]
    return {"prepared": prepared, "approved": approved, "queued": queued}


@app.post("/api/campaigns/{campaign_id}/pause", dependencies=[Depends(auth)])
def pause_campaign(campaign_id: int):
    with db.connect() as conn:
        result = conn.execute(
            "UPDATE outreach.campaigns SET status='paused',updated_at=now() "
            "WHERE id=%s AND status='active'",
            (campaign_id,),
        )
        conn.commit()
    if not result.rowcount:
        raise HTTPException(409, "Campanha não está ativa")
    return {"paused": True}


@app.post("/api/campaigns/{campaign_id}/resume", dependencies=[Depends(auth)])
def resume_campaign(campaign_id: int):
    with db.connect() as conn:
        result = conn.execute(
            "UPDATE outreach.campaigns SET status='active',updated_at=now() "
            "WHERE id=%s AND status='paused'",
            (campaign_id,),
        )
        conn.commit()
    if not result.rowcount:
        raise HTTPException(409, "Campanha não está pausada")
    return {"resumed": True}


@app.post("/api/campaigns/{campaign_id}/send-next", dependencies=[Depends(auth)])
def send_next_campaign_message(campaign_id: int):
    if settings.dry_run:
        raise HTTPException(409, "Envio real bloqueado enquanto DRY_RUN estiver ativo")
    try:
        settings.validate_smtp()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    with db.connect() as conn:
        status = process_one_result(conn, settings, campaign_id)
    return {"processed": status is not None, "status": status}


@app.post("/api/campaigns/{campaign_id}/send-batch", dependencies=[Depends(auth)])
def send_campaign_batch(
    campaign_id: int,
    limit: int = Query(default=50, ge=1, le=50),
):
    """Envia o saldo disponível da hora sem depender da aba do navegador."""
    if settings.dry_run:
        raise HTTPException(409, "Envio real bloqueado enquanto DRY_RUN estiver ativo")
    try:
        settings.validate_smtp()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    with db.connect() as conn:
        return process_batch(conn, settings, campaign_id, limit, 0)


@app.post("/api/dispatch/hourly")
def dispatch_hourly(x_cron_secret: str | None = Header(default=None)):
    """Ponto privado chamado pelo Supabase Cron; a cota é validada no banco."""
    if not settings.cron_secret or x_cron_secret != settings.cron_secret:
        raise HTTPException(401, "Cron não autorizado")
    if settings.dry_run:
        raise HTTPException(409, "Envio automático bloqueado enquanto DRY_RUN estiver ativo")
    try:
        settings.validate_smtp()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    with db.connect() as conn:
        return process_batch(conn, settings, None, settings.hourly_limit, 0)


@app.post("/api/campaigns/{campaign_id}/approve", dependencies=[Depends(auth)])
def approve_campaign(campaign_id: int):
    with db.connect() as conn:
        result = conn.execute(
            "UPDATE outreach.messages SET status='queued',approved_at=now(),updated_at=now() WHERE campaign_id=%s AND status='pending_approval'",
            (campaign_id,),
        )
        conn.execute(
            "UPDATE outreach.campaigns SET status='active',updated_at=now() WHERE id=%s",
            (campaign_id,),
        )
        conn.commit()
    return {"approved": result.rowcount}


@app.get("/api/dashboard", dependencies=[Depends(auth)])
def dashboard():
    with db.connect() as conn:
        return {
            "campaigns": conn.execute(
                "SELECT * FROM outreach.v_dashboard ORDER BY campaign_id DESC"
            ).fetchall()
        }


@app.get("/api/contacts", dependencies=[Depends(auth)])
def contacts(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=10, le=100),
    q: str = Query(default="", max_length=120),
):
    query_text = q.strip().lower()
    search = f"%{query_text}%"
    offset = (page - 1) * page_size
    search_expression = """
      lower(
        coalesce(l.trade_name,'') || ' ' || l.company_name || ' ' ||
        coalesce(l.email,'') || ' ' || l.cnpj::text
      ) ILIKE %s
    """
    search_clause = f"AND {search_expression}" if query_text else ""
    row_params = (search, page_size, offset) if query_text else (page_size, offset)
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
              l.id,
              coalesce(nullif(l.trade_name, ''), l.company_name) AS company,
              l.company_name,
              l.email,
              l.phone,
              l.whatsapp,
              coalesce(l.lead_score, 0) AS score,
              coalesce(l.confidence_score, 0) AS confidence_score,
              coalesce(l.source_payload->>'lead_quality', 'B') AS lead_quality,
              coalesce(nullif(l.source_payload #>> '{{sinais,intelligence_profile,profile_score}}', '')::numeric, 0) AS profile_score,
              coalesce(nullif(l.source_payload #>> '{{sinais,intelligence_profile,data_confidence_score}}', '')::numeric, 0) AS data_confidence_score,
              coalesce(l.source_payload->'qualification_reasons', '[]'::jsonb) AS qualification_reasons,
              l.status,
              latest.subject AS last_subject,
              coalesce((
                SELECT count(*)
                FROM outreach.events e
                WHERE e.message_id=latest.id
                  AND e.event_type IN ('open','opened')
              ), 0) AS opens
            FROM outreach.leads l
            LEFT JOIN LATERAL (
              SELECT m.id, m.subject
              FROM outreach.messages m
              WHERE m.lead_id=l.id
              ORDER BY m.created_at DESC, m.id DESC
              LIMIT 1
            ) latest ON true
            WHERE l.email IS NOT NULL
              {search_clause}
            ORDER BY l.id DESC
            LIMIT %s OFFSET %s
            """,
            row_params,
        ).fetchall()
        has_metrics = conn.execute(
            "SELECT to_regclass('outreach.lead_metrics') IS NOT NULL AS available"
        ).fetchone()["available"]
        if has_metrics:
            totals = conn.execute(
                """
                SELECT total,quality_a,quality_b,with_whatsapp,public_profile,
                  CASE WHEN total>0 THEN round(score_sum::numeric/total,1) ELSE 0 END
                    AS average_score
                FROM outreach.lead_metrics WHERE singleton
                """
            ).fetchone()
        else:
            # Compatibilidade durante o deploy, antes da migration de métricas.
            approximate = conn.execute(
                """
                SELECT greatest(reltuples,0)::bigint AS total
                FROM pg_class
                WHERE oid='outreach.leads'::regclass
                """
            ).fetchone()["total"]
            totals = {
                "total": approximate,
                "quality_a": 0,
                "quality_b": 0,
                "with_whatsapp": 0,
                "public_profile": 0,
                "average_score": 0,
            }
        filtered = totals["total"]
        if query_text:
            filtered = conn.execute(
                f"""
                SELECT count(*) AS total
                FROM outreach.leads l
                WHERE l.email IS NOT NULL AND {search_expression}
                """,
                (search,),
            ).fetchone()["total"]
    return {
        "contacts": rows,
        "metrics": totals,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": filtered,
            "total_pages": max(1, (filtered + page_size - 1) // page_size),
        },
    }


@app.get("/api/crm", dependencies=[Depends(auth)])
def crm_cases(q: str = Query(default="", max_length=120)):
    search = f"%{q.strip()}%"
    with db.connect() as conn:
        cases = conn.execute(
            """
            SELECT c.*, coalesce(nullif(l.trade_name, ''), l.company_name) AS company,
              l.company_name, l.cnpj, l.email, l.phone, l.whatsapp,
              coalesce(l.lead_score, 0) AS score,
              coalesce(l.source_payload->>'lead_quality', 'B') AS lead_quality
            FROM outreach.crm_cases c
            JOIN outreach.leads l ON l.id=c.lead_id
            WHERE coalesce(l.trade_name, '') ILIKE %s
              OR l.company_name ILIKE %s OR l.email ILIKE %s OR l.cnpj ILIKE %s
            ORDER BY c.updated_at DESC, c.id DESC
            """,
            (search, search, search, search),
        ).fetchall()
    return {"cases": cases}


@app.post("/api/crm", dependencies=[Depends(auth)], status_code=201)
def create_crm_case(data: CrmCaseIn):
    with db.connect() as conn:
        lead = conn.execute("SELECT id FROM outreach.leads WHERE id=%s", (data.lead_id,)).fetchone()
        if not lead:
            raise HTTPException(404, "Lead não encontrado")
        row = conn.execute(
            """
            INSERT INTO outreach.crm_cases
              (lead_id,service_type,owner_name,notes,next_action_at)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (lead_id) DO UPDATE SET
              service_type=EXCLUDED.service_type,
              owner_name=coalesce(EXCLUDED.owner_name,outreach.crm_cases.owner_name),
              notes=coalesce(EXCLUDED.notes,outreach.crm_cases.notes),
              next_action_at=coalesce(EXCLUDED.next_action_at,outreach.crm_cases.next_action_at),
              updated_at=now()
            RETURNING *
            """,
            (
                data.lead_id,
                data.service_type,
                data.owner_name,
                data.notes,
                data.next_action_at,
            ),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO outreach.crm_case_events (case_id,event_type,to_stage,note)
            VALUES (%s,'created',%s,%s)
            """,
            (row["id"], row["stage"], data.notes),
        )
        conn.execute(
            "UPDATE outreach.leads SET status=%s,updated_at=now() WHERE id=%s",
            (row["stage"], data.lead_id),
        )
        conn.commit()
    return {"case": row}


@app.patch("/api/crm/{case_id}", dependencies=[Depends(auth)])
def update_crm_case(case_id: int, data: CrmCaseUpdate):
    changes = data.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(422, "Nenhuma alteração informada")
    allowed = {"service_type", "stage", "owner_name", "notes", "next_action_at"}
    assignments = [f"{key}=%s" for key in changes if key in allowed]
    values = [changes[key] for key in changes if key in allowed]
    with db.connect() as conn:
        current = conn.execute(
            "SELECT * FROM outreach.crm_cases WHERE id=%s", (case_id,)
        ).fetchone()
        if not current:
            raise HTTPException(404, "Atendimento não encontrado")
        row = conn.execute(
            f"UPDATE outreach.crm_cases SET {', '.join(assignments)}, updated_at=now() WHERE id=%s RETURNING *",
            (*values, case_id),
        ).fetchone()
        if data.stage and data.stage != current["stage"]:
            conn.execute(
                """
                INSERT INTO outreach.crm_case_events
                  (case_id,event_type,from_stage,to_stage,note)
                VALUES (%s,'stage_changed',%s,%s,%s)
                """,
                (case_id, current["stage"], data.stage, data.notes),
            )
            conn.execute(
                "UPDATE outreach.leads SET status=%s,updated_at=now() WHERE id=%s",
                (data.stage, current["lead_id"]),
            )
        else:
            conn.execute(
                "INSERT INTO outreach.crm_case_events (case_id,event_type,note) VALUES (%s,'updated',%s)",
                (case_id, data.notes),
            )
        conn.commit()
    return {"case": row}


@app.get("/api/crm/leads/{lead_id}", dependencies=[Depends(auth)])
def crm_lead_detail(lead_id: int):
    with db.connect() as conn:
        lead = conn.execute(
            """
            SELECT l.*, coalesce(nullif(l.trade_name, ''), l.company_name) AS company,
              coalesce(l.lead_score, 0) AS score,
              coalesce(p.lead_quality, l.source_payload->>'lead_quality', 'B') AS lead_quality,
              coalesce(ip.profile_score, 0) AS profile_score,
              coalesce(ip.profile_quality, p.lead_quality) AS profile_quality,
              coalesce(ip.data_confidence_score, 0) AS data_confidence_score,
              v.natureza_juridica, v.natureza_juridica_descricao,
              v.porte, v.identificador_matriz_filial, v.situacao_cadastral,
              v.data_inicio_atividade, v.cnae_fiscal_principal,
              v.cnae_principal_descricao, v.cnaes_fiscais_secundarios,
              v.tipo_logradouro, v.logradouro, v.numero, v.complemento,
              v.bairro, v.cep, coalesce(p.uf, v.uf) AS uf,
              coalesce(p.municipio_descricao, v.municipio_descricao) AS municipio_descricao,
              p.site_url, p.site_final_url, p.site_ativo, p.plataforma,
              p.instagram_url, p.linkedin_url, p.digital_score, p.digital_maturity,
              p.presence_score, p.commerce_score, p.fit_score, p.pain_score,
              p.presence_maturity, p.commerce_maturity, p.lead_classification,
              p.decisor_nome, p.decisor_qualificacao,
              coalesce(ip.estimated_capacity_band, p.faixa_faturamento_estimada) AS faixa_faturamento_estimada,
              p.capital_social, p.opcao_mei, p.opcao_simples,
              p.qualification_status, p.qualification_reasons, p.rejection_reasons,
              p.contact_channel, p.contact_value, p.contact_confidence,
              p.qualification_version, p.qualified_at, p.last_qualified_at,
              ip.capacity_score, ip.intent_score, ip.decision_makers_count,
              ip.signals_count, ip.sources_success, ip.sources_pending,
              ip.summary AS intelligence_summary, ip.reasons AS intelligence_reasons,
              ip.intent_last_seen_at, ip.commercial_temperature,
              ip.last_commercial_event_at, ip.feedback_events_count,
              ev.deliverability_status, ev.risk_score AS email_risk_score,
              ev.mx_valid, ev.disposable AS email_disposable,
              ev.reason_codes AS email_reason_codes,
              gm.group_key, gm.is_primary AS group_primary
            FROM outreach.leads l
            LEFT JOIN cnpj.prospectos_qualificados p ON p.cnpj=l.cnpj
            LEFT JOIN cnpj.v_empresas_completas v ON v.cnpj=l.cnpj
            LEFT JOIN intelligence.company_profiles ip ON ip.cnpj=l.cnpj
            LEFT JOIN intelligence.email_verifications ev ON ev.cnpj=l.cnpj
            LEFT JOIN intelligence.company_group_members gm ON gm.cnpj=l.cnpj
            WHERE l.id=%s
            """,
            (lead_id,),
        ).fetchone()
        if not lead:
            raise HTTPException(404, "Lead não encontrado")
        case = conn.execute(
            "SELECT * FROM outreach.crm_cases WHERE lead_id=%s", (lead_id,)
        ).fetchone()
        messages = conn.execute(
            """
            SELECT m.id,m.channel,m.subject,m.status,m.sent_at,m.replied_at,m.updated_at,
              c.name AS campaign
            FROM outreach.messages m
            JOIN outreach.campaigns c ON c.id=m.campaign_id
            WHERE m.lead_id=%s ORDER BY m.created_at DESC LIMIT 20
            """,
            (lead_id,),
        ).fetchall()
        people = conn.execute(
            """
            SELECT id,full_name,role_title,relationship_type,linkedin_url,
              business_email,business_phone,is_decision_maker,confidence,
              source_code,source_url,priority_score
            FROM intelligence.company_people
            WHERE cnpj=%s AND active=true
            ORDER BY is_decision_maker DESC, priority_score DESC, confidence DESC
            LIMIT 25
            """,
            (lead["cnpj"],),
        ).fetchall()
        signals = conn.execute(
            """
            SELECT id,source_code,signal_type,category,title,description,score,
              confidence,observed_at,expires_at,source_url
            FROM intelligence.company_signals
            WHERE cnpj=%s AND (expires_at IS NULL OR expires_at>now())
            ORDER BY observed_at DESC, abs(score) DESC
            LIMIT 40
            """,
            (lead["cnpj"],),
        ).fetchall()
        technologies = conn.execute(
            """
            SELECT technology,category,confidence,source_code,source_url,observed_at
            FROM intelligence.company_technologies
            WHERE cnpj=%s AND active=true
            ORDER BY confidence DESC,technology
            LIMIT 30
            """,
            (lead["cnpj"],),
        ).fetchall()
        sources = conn.execute(
            """
            SELECT state.source_code,registry.display_name,state.status,state.records_found,
              state.last_checked_at,state.last_error
            FROM intelligence.company_source_state state
            LEFT JOIN intelligence.source_registry registry
              ON registry.source_code=state.source_code
            WHERE state.cnpj=%s
            ORDER BY registry.display_name NULLS LAST,state.source_code
            """,
            (lead["cnpj"],),
        ).fetchall()
        history = []
        if case:
            history = conn.execute(
                "SELECT * FROM outreach.crm_case_events WHERE case_id=%s ORDER BY created_at DESC LIMIT 50",
                (case["id"],),
            ).fetchall()
    return {
        "lead": lead,
        "case": case,
        "messages": messages,
        "history": history,
        "people": people,
        "signals": signals,
        "technologies": technologies,
        "sources": sources,
    }


@app.get("/api/queue", dependencies=[Depends(auth)])
def queue_status():
    with db.connect() as conn:
        ensure_dispatch_schema(conn)
        metrics = conn.execute(
            """
            SELECT
              count(*) FILTER (
                WHERE status IN ('pending_approval','approved','queued','sending')
              ) AS queued,
              count(*) FILTER (WHERE sent_at >= current_date) AS processed_today,
              count(*) FILTER (
                WHERE sent_at >= current_date
                  AND status IN ('sent','delivered','replied','unsubscribed')
              ) AS accepted_today,
              count(*) FILTER (
                WHERE updated_at >= current_date AND status IN ('failed','bounced')
              ) AS failed_today
            FROM outreach.messages
            """
        ).fetchone()
        items = conn.execute(
            """
            SELECT
              m.id,
              coalesce(nullif(l.trade_name, ''), l.company_name) AS company,
              c.name AS campaign,
              c.id AS campaign_id,
              c.status AS campaign_status,
              t.name AS template_name,
              m.destination,
              m.subject,
              m.scheduled_at,
              m.sent_at,
              m.status,
              m.updated_at,
              m.provider,
              m.last_error,
              coalesce(l.lead_score,0) AS score,
              coalesce(l.confidence_score,0) AS confidence_score,
              coalesce(l.contact_role,'general') AS contact_role,
              coalesce(l.source_payload->>'lead_quality','B') AS lead_quality
            FROM outreach.messages m
            JOIN outreach.leads l ON l.id=m.lead_id
            JOIN outreach.campaigns c ON c.id=m.campaign_id
            LEFT JOIN outreach.templates t ON t.id=c.template_id
            WHERE m.status <> 'canceled'
            ORDER BY
              CASE WHEN m.status IN ('sending','approved','queued','pending_approval')
                THEN 0 ELSE 1 END,
              CASE coalesce(l.source_payload->>'lead_quality','B')
                WHEN 'A' THEN 0 ELSE 1 END,
              l.lead_score DESC NULLS LAST,
              l.confidence_score DESC NULLS LAST,
              CASE l.contact_role WHEN 'sales' THEN 0 WHEN 'general' THEN 1 ELSE 2 END,
              m.updated_at DESC,
              m.id DESC
            LIMIT 200
            """
        ).fetchall()
        campaigns = conn.execute(
            """
            SELECT c.id,c.name,c.status,c.audience_mode,c.audience_qualities,
              c.min_score,c.max_score,c.daily_limit,c.scheduled_start_at,c.launched_at,
              t.name AS template_name,
              count(m.id) AS total,
              count(*) FILTER (
                WHERE m.status IN ('pending_approval','approved','queued')
              ) AS queued,
              count(*) FILTER (WHERE m.status='sending') AS sending,
              count(*) FILTER (
                WHERE m.status IN ('sent','delivered','replied','unsubscribed')
              ) AS sent,
              count(*) FILTER (WHERE m.status='delivered') AS delivered,
              count(*) FILTER (WHERE m.status IN ('failed','bounced')) AS failed,
              count(*) FILTER (WHERE m.status='replied') AS replied
            FROM outreach.campaigns c
            LEFT JOIN outreach.templates t ON t.id=c.template_id
            LEFT JOIN outreach.messages m ON m.campaign_id=c.id
            GROUP BY c.id,t.name
            ORDER BY c.updated_at DESC,c.id DESC
            LIMIT 30
            """
        ).fetchall()
        recent = conn.execute(
            """
            SELECT m.id, m.destination, m.status, m.last_error, m.updated_at
            FROM outreach.messages m
            ORDER BY m.updated_at DESC, m.id DESC
            LIMIT 20
            """
        ).fetchall()
    logs = [
        {
            "id": row["id"],
            "time": row["updated_at"].isoformat(),
            "level": "error" if row["status"] in {"failed", "bounced"} else "success",
            "message": row["last_error"] or f"Mensagem {row['status']} para {row['destination']}",
        }
        for row in recent
    ]
    return {"metrics": metrics, "campaigns": campaigns, "items": items, "logs": logs}


@app.get("/api/settings", dependencies=[Depends(auth)])
def operational_settings():
    return {
        "settings": {
            "provider": settings.email_provider,
            "from_name": settings.from_name,
            "from_email": settings.from_email,
            "reply_to": settings.reply_to,
            "require_approval": settings.require_approval,
            "daily_limit": settings.daily_limit,
            "hourly_limit": settings.hourly_limit,
            "domain_daily_limit": settings.domain_daily_limit,
            "send_interval_seconds": settings.send_interval_seconds,
            "send_start_hour": settings.send_start_hour,
            "send_end_hour": settings.send_end_hour,
            "timezone": settings.timezone,
            "dry_run": settings.dry_run,
        }
    }


@app.post("/api/webhooks/sendpulse")
def sendpulse_webhook(events: list[dict], secret: str | None = None):
    if not settings.sendpulse_webhook_secret:
        raise HTTPException(503, "Webhook SendPulse não configurado")
    if secret != settings.sendpulse_webhook_secret:
        raise HTTPException(401, "Assinatura inválida")

    accepted = 0
    suppressed = 0
    safety_pause: dict | None = None
    with db.connect() as conn:
        for event in events:
            event_type = normalize_sendpulse_event(event.get("event"))
            if not event_type:
                continue
            recipient = str(event.get("recipient") or event.get("email") or "")
            provider_message_id = str(event.get("message_id") or event.get("task_id") or "")
            message = None
            if provider_message_id:
                message = conn.execute(
                    "SELECT id,lead_id FROM outreach.messages WHERE provider_message_id=%s ORDER BY id DESC LIMIT 1",
                    (provider_message_id,),
                ).fetchone()
            if not message and recipient:
                message = conn.execute(
                    "SELECT id,lead_id FROM outreach.messages WHERE lower(destination)=lower(%s) ORDER BY created_at DESC LIMIT 1",
                    (recipient,),
                ).fetchone()
            if not message:
                continue

            provider_event_id = ":".join(
                [
                    event_type,
                    provider_message_id,
                    recipient,
                    str(event.get("timestamp", "")),
                ]
            )
            inserted = conn.execute(
                """
                INSERT INTO outreach.events
                  (message_id,event_type,provider_event_id,payload,occurred_at)
                VALUES (%s,%s,%s,%s::jsonb,coalesce(to_timestamp(%s),now()))
                ON CONFLICT (provider_event_id) DO NOTHING
                """,
                (
                    message["id"],
                    event_type,
                    provider_event_id,
                    json.dumps(event),
                    int(event.get("timestamp") or 0) or None,
                ),
            )
            if not inserted.rowcount:
                continue
            accepted += 1
            if event_type == "delivered":
                conn.execute(
                    "UPDATE outreach.messages SET status='delivered',delivered_at=now(),updated_at=now() WHERE id=%s",
                    (message["id"],),
                )
            elif event_type in {"undelivered", "bounce", "hard_bounce", "soft_bounce"}:
                conn.execute(
                    "UPDATE outreach.messages SET status='bounced',updated_at=now() WHERE id=%s",
                    (message["id"],),
                )
            elif event_type == "spam":
                conn.execute(
                    "UPDATE outreach.messages SET status='blocked',updated_at=now() WHERE id=%s",
                    (message["id"],),
                )
            elif event_type == "unsubscribed":
                conn.execute(
                    "UPDATE outreach.messages SET status='unsubscribed',updated_at=now() WHERE id=%s",
                    (message["id"],),
                )

            suppression_reason = permanent_suppression_reason(event_type)
            if suppression_reason and recipient:
                conn.execute(
                    """
                    INSERT INTO outreach.suppressions
                      (channel,destination,reason,source,permanent)
                    VALUES ('email',lower(%s),%s,'sendpulse_webhook',true)
                    ON CONFLICT (channel,destination) DO UPDATE SET
                      reason=EXCLUDED.reason,source=EXCLUDED.source,permanent=true
                    """,
                    (recipient, suppression_reason),
                )
                conn.execute(
                    "UPDATE outreach.leads SET status='blocked',updated_at=now() WHERE id=%s",
                    (message["lead_id"],),
                )
                suppressed += 1

        row = conn.execute(
            """
            SELECT
              count(DISTINCT m.id) FILTER (
                WHERE sent_at >= now() - interval '15 minutes'
              ) AS sent_15m,
              count(DISTINCT e.message_id) FILTER (
                WHERE e.occurred_at >= now() - interval '15 minutes'
                  AND e.event_type IN ('undelivered','bounce','hard_bounce','soft_bounce')
              ) AS bounced_15m,
              count(DISTINCT m.id) FILTER (
                WHERE sent_at >= now() - interval '24 hours'
              ) AS sent_24h,
              count(DISTINCT e.message_id) FILTER (
                WHERE e.occurred_at >= now() - interval '24 hours'
                  AND e.event_type='spam'
              ) AS complaints_24h
            FROM outreach.messages m
            LEFT JOIN outreach.events e ON e.message_id=m.id
            """
        ).fetchone()
        snapshot = ReputationSnapshot(
            sent_15m=int(row["sent_15m"] or 0),
            bounced_15m=int(row["bounced_15m"] or 0),
            sent_24h=int(row["sent_24h"] or 0),
            complaints_24h=int(row["complaints_24h"] or 0),
        )
        pause_reasons = reputation_pause_reasons(
            snapshot,
            minimum_sample=settings.reputation_min_sample,
            max_bounce_rate_percent=settings.max_bounce_rate_percent,
            max_complaint_rate_percent=settings.max_complaint_rate_percent,
        )
        if pause_reasons:
            paused = conn.execute(
                "UPDATE outreach.campaigns SET status='paused',updated_at=now() WHERE status='active'"
            ).rowcount
            safety_pause = {
                "reasons": pause_reasons,
                "campaigns_paused": paused,
                "bounce_rate_percent": round(snapshot.bounce_rate_percent, 3),
                "complaint_rate_percent": round(snapshot.complaint_rate_percent, 3),
            }
        conn.commit()
    return {
        "received": len(events),
        "accepted": accepted,
        "suppressed": suppressed,
        "safety_pause": safety_pause,
    }


@app.post("/api/messages/{message_id}/reply", dependencies=[Depends(auth)])
def mark_reply(message_id: int):
    with db.connect() as conn:
        message = conn.execute(
            "SELECT lead_id FROM outreach.messages WHERE id=%s",
            (message_id,),
        ).fetchone()
        if not message:
            raise HTTPException(404, "Mensagem não encontrada")
        conn.execute(
            """
            UPDATE outreach.messages
            SET status='replied',replied_at=now(),updated_at=now()
            WHERE id=%s
            """,
            (message_id,),
        )
        conn.execute(
            """
            UPDATE outreach.leads
            SET status='replied',updated_at=now()
            WHERE id=%s
            """,
            (message["lead_id"],),
        )
        conn.commit()
    return {"replied": True}


@app.post("/api/messages/{message_id}/resolve-delivery", dependencies=[Depends(auth)])
def resolve_delivery(message_id: int, data: DeliveryResolutionIn):
    with db.connect() as conn:
        message = conn.execute(
            """
            SELECT lead_id,status FROM outreach.messages WHERE id=%s
            """,
            (message_id,),
        ).fetchone()
        if not message:
            raise HTTPException(404, "Mensagem não encontrada")
        if message["status"] != "delivery_uncertain":
            raise HTTPException(409, "Mensagem não está com entrega incerta")
        if data.delivered:
            conn.execute(
                """
                UPDATE outreach.messages
                SET status='sent',sent_at=coalesce(sent_at,now()),updated_at=now()
                WHERE id=%s
                """,
                (message_id,),
            )
            conn.execute(
                """
                UPDATE outreach.leads
                SET status='contacted',updated_at=now()
                WHERE id=%s AND status='ready'
                """,
                (message["lead_id"],),
            )
        else:
            conn.execute(
                """
                UPDATE outreach.messages
                SET status='failed',last_error='Entrega descartada manualmente',
                    updated_at=now()
                WHERE id=%s
                """,
                (message_id,),
            )
        conn.commit()
    return {"resolved": True, "delivered": data.delivered}


@app.api_route(
    "/unsubscribe/{message_id}", methods=["GET", "POST"], response_class=PlainTextResponse
)
def unsubscribe(message_id: int, token: str):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT destination FROM outreach.messages WHERE id=%s", (message_id,)
        ).fetchone()
        if (
            not row
            or not settings.unsubscribe_secret
            or not valid_unsubscribe_token(
                message_id, row["destination"], token, settings.unsubscribe_secret
            )
        ):
            raise HTTPException(400, "Link inválido")
        conn.execute(
            "INSERT INTO outreach.suppressions (channel,destination,reason,source) VALUES ('email',%s,'unsubscribe','recipient') ON CONFLICT (channel,destination) DO UPDATE SET reason='unsubscribe',source='recipient'",
            (row["destination"],),
        )
        conn.execute(
            "UPDATE outreach.messages SET status='unsubscribed',updated_at=now() WHERE id=%s",
            (message_id,),
        )
        conn.execute(
            """
            UPDATE outreach.leads SET status='blocked',updated_at=now()
            WHERE id=(SELECT lead_id FROM outreach.messages WHERE id=%s)
            """,
            (message_id,),
        )
        conn.commit()
    return "Descadastro realizado. Este endereço não receberá novas mensagens."
