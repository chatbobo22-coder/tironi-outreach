-- Mantém a aba de contatos rápida com métricas pré-calculadas. A paginação
-- usa a chave primária já existente; evitamos um GIN volumoso enquanto o
-- banco está acima da cota.

CREATE INDEX IF NOT EXISTS idx_messages_lead_latest
  ON outreach.messages (lead_id,created_at DESC,id DESC);

CREATE INDEX IF NOT EXISTS idx_events_message_open
  ON outreach.events (message_id,event_type)
  WHERE event_type IN ('open','opened');

CREATE TABLE IF NOT EXISTS outreach.lead_metrics (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  total bigint NOT NULL DEFAULT 0,
  quality_a bigint NOT NULL DEFAULT 0,
  quality_b bigint NOT NULL DEFAULT 0,
  with_whatsapp bigint NOT NULL DEFAULT 0,
  public_profile bigint NOT NULL DEFAULT 0,
  score_sum bigint NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO outreach.lead_metrics
  (singleton,total,quality_a,quality_b,with_whatsapp,public_profile,score_sum,updated_at)
SELECT true,
  count(*) FILTER (WHERE email IS NOT NULL),
  count(*) FILTER (WHERE email IS NOT NULL AND source_payload->>'lead_quality'='A'),
  count(*) FILTER (WHERE email IS NOT NULL AND source_payload->>'lead_quality'='B'),
  count(*) FILTER (WHERE email IS NOT NULL AND NULLIF(whatsapp,'') IS NOT NULL),
  count(*) FILTER (
    WHERE email IS NOT NULL
      AND source_payload->'qualification_reasons' ? 'perfil_publico_verificado'
  ),
  COALESCE(sum(COALESCE(lead_score,0)) FILTER (WHERE email IS NOT NULL),0),
  now()
FROM outreach.leads
WHERE NOT EXISTS (SELECT 1 FROM outreach.lead_metrics WHERE singleton)
ON CONFLICT (singleton) DO NOTHING;
