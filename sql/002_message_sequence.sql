ALTER TABLE outreach.messages
  ADD COLUMN IF NOT EXISTS sequence_step smallint NOT NULL DEFAULT 0;

ALTER TABLE outreach.campaigns
  ADD COLUMN IF NOT EXISTS campaign_key text;

CREATE UNIQUE INDEX IF NOT EXISTS uq_campaigns_campaign_key
  ON outreach.campaigns (campaign_key)
  WHERE campaign_key IS NOT NULL;

ALTER TABLE outreach.messages
  DROP CONSTRAINT IF EXISTS messages_campaign_id_lead_id_channel_key;

CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_campaign_lead_channel_step
  ON outreach.messages (campaign_id, lead_id, channel, sequence_step);

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid='outreach.messages'::regclass
      AND conname='messages_status_check'
      AND pg_get_constraintdef(oid) NOT LIKE '%delivery_uncertain%'
  ) THEN
    ALTER TABLE outreach.messages
      DROP CONSTRAINT messages_status_check;
    ALTER TABLE outreach.messages
      ADD CONSTRAINT messages_status_check CHECK (status IN
        ('draft','pending_approval','approved','queued','sending','sent','delivered',
         'failed','delivery_uncertain','bounced','replied','unsubscribed','blocked',
         'canceled'));
  END IF;
END
$$;

DO $$
BEGIN
  ALTER TABLE outreach.messages
    ADD CONSTRAINT messages_sequence_step_check
    CHECK (sequence_step IN (0, 1));
EXCEPTION
  WHEN duplicate_object THEN NULL;
END
$$;
