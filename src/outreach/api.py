import json

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .config import Settings
from .db import Database
from .security import valid_unsubscribe_token
from .service import prepare_campaign, sync_leads

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
    daily_limit: int = Field(default=30, ge=1, le=1000)


class TemplateIn(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    subject: str = Field(min_length=3, max_length=200)
    preheader: str = Field(default="", max_length=240)
    text_body: str = Field(min_length=10, max_length=10000)
    html_body: str = Field(min_length=10, max_length=100000)


class DeliveryResolutionIn(BaseModel):
    delivered: bool


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
    with db.connect() as conn:
        row = conn.execute(
            "INSERT INTO outreach.campaigns "
            "(name,subject_template,body_template,body_html_template,template_id,daily_limit,requires_approval) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                data.name,
                data.subject_template,
                data.body_template,
                data.body_html_template,
                data.template_id,
                data.daily_limit,
                settings.require_approval,
            ),
        ).fetchone()
        conn.commit()
    return {"id": row["id"]}


@app.get("/api/templates", dependencies=[Depends(auth)])
def list_templates():
    with db.connect() as conn:
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


@app.post("/api/webhooks/sendpulse")
def sendpulse_webhook(events: list[dict], secret: str | None = None):
    if settings.sendpulse_webhook_secret and secret != settings.sendpulse_webhook_secret:
        raise HTTPException(401, "Assinatura inválida")

    accepted = 0
    with db.connect() as conn:
        for event in events:
            event_type = str(event.get("event", "")).lower()
            if event_type not in {
                "delivered",
                "undelivered",
                "bounce",
                "hard_bounce",
                "soft_bounce",
                "open",
                "opened",
                "click",
                "clicked",
            }:
                continue
            recipient = str(event.get("recipient") or event.get("email") or "")
            provider_message_id = str(event.get("message_id") or event.get("task_id") or "")
            message = None
            if provider_message_id:
                message = conn.execute(
                    "SELECT id FROM outreach.messages WHERE provider_message_id=%s ORDER BY id DESC LIMIT 1",
                    (provider_message_id,),
                ).fetchone()
            if not message and recipient:
                message = conn.execute(
                    "SELECT id FROM outreach.messages WHERE destination=%s ORDER BY created_at DESC LIMIT 1",
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
            elif event_type in {
                "undelivered",
                "bounce",
                "hard_bounce",
                "soft_bounce",
            }:
                conn.execute(
                    "UPDATE outreach.messages SET status='bounced',updated_at=now() WHERE id=%s",
                    (message["id"],),
                )
        conn.commit()
    return {"received": len(events), "accepted": accepted}


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
        conn.commit()
    return "Descadastro realizado. Este endereço não receberá novas mensagens."
