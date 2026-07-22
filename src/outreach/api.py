from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
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


class DeliveryResolutionIn(BaseModel):
    delivered: bool


@app.on_event("startup")
def startup():
    db.migrate(Path(__file__).resolve().parents[2] / "sql")


@app.get("/health")
def health():
    return {"status": "ok", "dry_run": settings.dry_run}


@app.post("/api/leads/sync", dependencies=[Depends(auth)])
def api_sync_leads():
    with db.connect() as conn:
        return {"synced": sync_leads(conn)}


@app.post("/api/campaigns", dependencies=[Depends(auth)])
def create_campaign(data: CampaignIn):
    with db.connect() as conn:
        row = conn.execute(
            "INSERT INTO outreach.campaigns (name,subject_template,body_template,requires_approval) VALUES (%s,%s,%s,%s) RETURNING id",
            (data.name, data.subject_template, data.body_template, settings.require_approval),
        ).fetchone()
        conn.commit()
    return {"id": row["id"]}


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
