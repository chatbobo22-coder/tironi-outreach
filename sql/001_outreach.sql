CREATE SCHEMA IF NOT EXISTS outreach;

CREATE TABLE IF NOT EXISTS outreach.leads (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  cnpj char(14) NOT NULL UNIQUE,
  company_name text NOT NULL,
  trade_name text,
  email text,
  email_domain text,
  phone text,
  whatsapp text,
  contact_role text NOT NULL DEFAULT 'general',
  lead_score smallint,
  confidence_score smallint,
  source text NOT NULL DEFAULT 'cnpj_etl',
  status text NOT NULL DEFAULT 'new' CHECK (status IN
    ('new','reviewing','ready','contacted','replied','qualified','meeting','proposal','won','lost','blocked')),
  source_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS outreach.campaigns (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name text NOT NULL,
  channel text NOT NULL DEFAULT 'email' CHECK (channel IN ('email','whatsapp','instagram','facebook','linkedin')),
  subject_template text,
  body_template text NOT NULL,
  status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','paused','completed','canceled')),
  requires_approval boolean NOT NULL DEFAULT true,
  daily_limit integer NOT NULL DEFAULT 30 CHECK (daily_limit > 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS outreach.messages (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  campaign_id bigint NOT NULL REFERENCES outreach.campaigns(id),
  lead_id bigint NOT NULL REFERENCES outreach.leads(id),
  channel text NOT NULL,
  destination text NOT NULL,
  destination_domain text,
  subject text,
  body_text text NOT NULL,
  status text NOT NULL DEFAULT 'pending_approval' CHECK (status IN
    ('draft','pending_approval','approved','queued','sending','sent','delivered','failed','bounced','replied','unsubscribed','blocked','canceled')),
  attempt_count integer NOT NULL DEFAULT 0,
  provider text,
  provider_message_id text,
  scheduled_at timestamptz,
  approved_at timestamptz,
  sent_at timestamptz,
  delivered_at timestamptz,
  replied_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (campaign_id, lead_id, channel)
);

CREATE TABLE IF NOT EXISTS outreach.suppressions (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  channel text NOT NULL,
  destination text NOT NULL,
  reason text NOT NULL,
  source text NOT NULL DEFAULT 'system',
  permanent boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (channel, destination)
);

CREATE TABLE IF NOT EXISTS outreach.events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  message_id bigint REFERENCES outreach.messages(id),
  event_type text NOT NULL,
  provider_event_id text,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider_event_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_queue ON outreach.messages (status, scheduled_at, id);
CREATE INDEX IF NOT EXISTS idx_messages_sent_at ON outreach.messages (sent_at);
CREATE INDEX IF NOT EXISTS idx_messages_domain_sent ON outreach.messages (destination_domain, sent_at);
CREATE INDEX IF NOT EXISTS idx_leads_status_score ON outreach.leads (status, lead_score DESC);

CREATE OR REPLACE VIEW outreach.v_dashboard AS
SELECT
  c.id AS campaign_id, c.name, c.channel, c.status AS campaign_status,
  count(m.id) AS messages_total,
  count(*) FILTER (WHERE m.status = 'pending_approval') AS pending_approval,
  count(*) FILTER (WHERE m.status IN ('approved','queued')) AS queued,
  count(*) FILTER (WHERE m.status IN ('sent','delivered')) AS sent,
  count(*) FILTER (WHERE m.status = 'bounced') AS bounced,
  count(*) FILTER (WHERE m.status = 'replied') AS replied,
  count(*) FILTER (WHERE m.status = 'unsubscribed') AS unsubscribed
FROM outreach.campaigns c
LEFT JOIN outreach.messages m ON m.campaign_id = c.id
GROUP BY c.id;

