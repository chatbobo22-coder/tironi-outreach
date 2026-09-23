CREATE TABLE IF NOT EXISTS outreach.crm_cases (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lead_id bigint NOT NULL UNIQUE REFERENCES outreach.leads(id) ON DELETE CASCADE,
  service_type text NOT NULL CHECK (service_type IN
    ('prospecting','qualification','demo','proposal','negotiation','follow_up','reactivation','after_sales')),
  stage text NOT NULL DEFAULT 'ready' CHECK (stage IN
    ('ready','contacted','replied','qualified','meeting','proposal','won','lost')),
  owner_name text,
  notes text,
  next_action_at timestamptz,
  started_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS outreach.crm_case_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  case_id bigint NOT NULL REFERENCES outreach.crm_cases(id) ON DELETE CASCADE,
  event_type text NOT NULL CHECK (event_type IN ('created','stage_changed','updated','note')),
  from_stage text,
  to_stage text,
  note text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_crm_cases_stage_updated
  ON outreach.crm_cases (stage, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_crm_case_events_case_created
  ON outreach.crm_case_events (case_id, created_at DESC);
