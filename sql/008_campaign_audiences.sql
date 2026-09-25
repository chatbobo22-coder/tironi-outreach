ALTER TABLE outreach.campaigns
  ADD COLUMN IF NOT EXISTS audience_mode text NOT NULL DEFAULT 'all',
  ADD COLUMN IF NOT EXISTS audience_qualities text[] NOT NULL DEFAULT ARRAY['A','B']::text[],
  ADD COLUMN IF NOT EXISTS min_score smallint,
  ADD COLUMN IF NOT EXISTS max_score smallint,
  ADD COLUMN IF NOT EXISTS scheduled_start_at timestamptz,
  ADD COLUMN IF NOT EXISTS launched_at timestamptz;

DO $$
BEGIN
  ALTER TABLE outreach.campaigns
    ADD CONSTRAINT campaigns_audience_mode_check
    CHECK (audience_mode IN ('all','quality','score'));
EXCEPTION
  WHEN duplicate_object THEN NULL;
END
$$;

DO $$
BEGIN
  ALTER TABLE outreach.campaigns
    ADD CONSTRAINT campaigns_score_range_check
    CHECK (
      (min_score IS NULL OR min_score BETWEEN 0 AND 100)
      AND (max_score IS NULL OR max_score BETWEEN 0 AND 100)
      AND (min_score IS NULL OR max_score IS NULL OR min_score <= max_score)
    );
EXCEPTION
  WHEN duplicate_object THEN NULL;
END
$$;
