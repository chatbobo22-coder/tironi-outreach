-- Relógio confiável para o envio horário. O segredo deve existir no Vault com
-- o nome outreach_cron_secret; seu valor nunca fica armazenado nesta migration.
CREATE EXTENSION IF NOT EXISTS pg_cron WITH SCHEMA pg_catalog;
CREATE EXTENSION IF NOT EXISTS pg_net WITH SCHEMA extensions;

SELECT cron.schedule(
  'tironi-outreach-hourly-dispatch',
  '7,22,37,52 12-19 * * 1-5',
  $cron$
    SELECT net.http_post(
      url := 'https://tironi-outreach.vercel.app/api/dispatch/hourly',
      headers := jsonb_build_object(
        'Content-Type', 'application/json',
        'X-Cron-Secret', (
          SELECT decrypted_secret
          FROM vault.decrypted_secrets
          WHERE name='outreach_cron_secret'
          LIMIT 1
        )
      ),
      body := jsonb_build_object('triggered_at', now()),
      timeout_milliseconds := 120000
    ) AS request_id;
  $cron$
);

-- O histórico do pg_cron não é limpo automaticamente.
SELECT cron.schedule(
  'tironi-outreach-cron-history-cleanup',
  '30 3 * * *',
  $$DELETE FROM cron.job_run_details WHERE end_time < now() - interval '7 days'$$
);
