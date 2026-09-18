CREATE TABLE IF NOT EXISTS outreach.templates (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name text NOT NULL,
  subject text NOT NULL,
  preheader text NOT NULL DEFAULT '',
  text_body text NOT NULL,
  html_body text NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE outreach.campaigns
  ADD COLUMN IF NOT EXISTS template_id bigint REFERENCES outreach.templates(id);

CREATE INDEX IF NOT EXISTS idx_templates_updated_at
  ON outreach.templates (updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_campaigns_template_id
  ON outreach.campaigns (template_id);

CREATE OR REPLACE VIEW outreach.v_dashboard AS
SELECT
  c.id AS campaign_id,
  c.name,
  c.channel,
  c.status AS campaign_status,
  c.template_id,
  t.name AS template_name,
  c.subject_template,
  c.daily_limit,
  c.created_at,
  c.updated_at,
  count(m.id) AS messages_total,
  count(*) FILTER (WHERE m.status = 'pending_approval') AS pending_approval,
  count(*) FILTER (WHERE m.status IN ('approved','queued')) AS queued,
  count(*) FILTER (WHERE m.status IN ('sent','delivered')) AS sent,
  count(*) FILTER (WHERE m.status = 'delivered') AS delivered,
  count(*) FILTER (WHERE m.status = 'bounced') AS bounced,
  count(*) FILTER (WHERE m.status = 'replied') AS replied,
  count(*) FILTER (WHERE m.status = 'unsubscribed') AS unsubscribed,
  count(*) FILTER (
    WHERE EXISTS (
      SELECT 1 FROM outreach.events e
      WHERE e.message_id=m.id AND e.event_type IN ('open','opened')
    )
  ) AS opened,
  count(*) FILTER (
    WHERE EXISTS (
      SELECT 1 FROM outreach.events e
      WHERE e.message_id=m.id AND e.event_type IN ('click','clicked')
    )
  ) AS clicked
FROM outreach.campaigns c
LEFT JOIN outreach.templates t ON t.id = c.template_id
LEFT JOIN outreach.messages m ON m.campaign_id = c.id
GROUP BY c.id, t.name;
