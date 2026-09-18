ALTER TABLE outreach.campaigns
  ADD COLUMN IF NOT EXISTS body_html_template text;

ALTER TABLE outreach.messages
  ADD COLUMN IF NOT EXISTS body_html text;
