-- Atualiza campanhas criadas pelo formulário antigo, cujo padrão era 30/dia.
-- Apenas campanhas ainda sem nenhum envio são ajustadas; limites escolhidos em
-- campanhas já executadas permanecem intactos.
UPDATE outreach.campaigns c
SET daily_limit = 400,
    updated_at = now()
WHERE c.daily_limit = 30
  AND c.status IN ('draft', 'active')
  AND NOT EXISTS (
    SELECT 1
    FROM outreach.messages m
    WHERE m.campaign_id = c.id
      AND m.status IN ('sending', 'sent', 'delivered', 'replied', 'unsubscribed')
  );

-- Reclassifica caixas operacionais que o classificador antigo só reconhecia
-- quando o endereço era exatamente "fiscal@" ou "financeiro@".
UPDATE outreach.leads
SET contact_role = 'finance',
    updated_at = now()
WHERE split_part(lower(email),'@',1)
        ~ '(^|[._+-])(financeiro|financeira|fiscal|nfe|nfse|contabilidade|contabil|contaspagar|contasapagar|contaapagar|cobranca|juridico|rh)([._+-]|$)'
   OR split_part(lower(email),'@',2) LIKE '%contabil%';

UPDATE outreach.messages m
SET status = 'canceled',
    last_error = 'Destinatário operacional removido da priorização comercial',
    updated_at = now()
FROM outreach.leads l
WHERE l.id = m.lead_id
  AND l.contact_role IN ('finance', 'accounting')
  AND m.status IN ('pending_approval', 'approved', 'queued');

CREATE INDEX IF NOT EXISTS idx_messages_hourly_priority
  ON outreach.messages (status, scheduled_at, campaign_id, id)
  WHERE status IN ('approved', 'queued');
