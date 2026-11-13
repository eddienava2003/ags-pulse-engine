-- GENRAI-1 queue alert email notifications.
-- Sends one Resend email when a queue alert is first created or reactivated.
-- Does not change event processing, queue retry behavior, or read-only monitor behavior.

CREATE TABLE IF NOT EXISTS genrai_ops.queue_alert_notifications (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  alert_key text NOT NULL,
  activated_at timestamptz NOT NULL,
  recipient text NOT NULL,
  severity text NOT NULL,
  status text NOT NULL DEFAULT 'QUEUED',
  request_id bigint,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (alert_key, activated_at)
);

CREATE OR REPLACE FUNCTION genrai_ops.notify_queue_alert_email()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'pg_catalog', 'genrai_ops', 'net', 'vault'
AS $function$
DECLARE
  v_api_key text;
  v_request_id bigint;
  v_recipient constant text := 'eddienava2003@yahoo.com';
  v_subject text;
  v_body text;
BEGIN
  -- Notify only when an alert is first created or transitions from resolved to active.
  IF TG_OP = 'UPDATE' AND NOT (OLD.resolved_at IS NOT NULL AND NEW.resolved_at IS NULL) THEN
    RETURN NEW;
  END IF;
  IF NEW.resolved_at IS NOT NULL THEN
    RETURN NEW;
  END IF;

  IF EXISTS (
    SELECT 1 FROM genrai_ops.queue_alert_notifications n
    WHERE n.alert_key = NEW.alert_key
      AND n.activated_at = NEW.last_seen_at
  ) THEN
    RETURN NEW;
  END IF;

  SELECT decrypted_secret INTO v_api_key
  FROM vault.decrypted_secrets
  WHERE name = 'resend_api_key'
  LIMIT 1;

  IF v_api_key IS NULL OR length(v_api_key) = 0 THEN
    INSERT INTO genrai_ops.queue_alert_notifications
      (alert_key, activated_at, recipient, severity, status, error)
    VALUES
      (NEW.alert_key, NEW.last_seen_at, v_recipient, NEW.severity, 'FAILED',
       'Resend API key missing from Supabase Vault')
    ON CONFLICT (alert_key, activated_at) DO NOTHING;
    RETURN NEW;
  END IF;

  v_subject := '[GENRAI-1 ' || upper(NEW.severity) || '] ' || left(NEW.message, 140);
  v_body := 'GENRAI-1 queue alert' || E'\n\n'
    || 'Severity: ' || upper(NEW.severity) || E'\n'
    || 'Alert: ' || NEW.alert_key || E'\n'
    || 'Message: ' || NEW.message || E'\n'
    || 'Observed: ' || NEW.last_seen_at::text || E'\n'
    || 'Occurrences: ' || NEW.occurrence_count::text || E'\n'
    || 'Details: ' || coalesce(NEW.details::text, '{}') || E'\n\n'
    || 'This is an automated advisory. Validate operational conditions before taking action.';

  BEGIN
    v_request_id := net.http_post(
      url := 'https://api.resend.com/emails',
      body := jsonb_build_object(
        'from', 'AGS-Pulse <alerts@ags-pulse.com>',
        'to', jsonb_build_array(v_recipient),
        'subject', v_subject,
        'text', v_body
      ),
      params := '{}'::jsonb,
      headers := jsonb_build_object(
        'Authorization', 'Bearer ' || v_api_key,
        'Content-Type', 'application/json'
      ),
      timeout_milliseconds := 10000
    );

    INSERT INTO genrai_ops.queue_alert_notifications
      (alert_key, activated_at, recipient, severity, status, request_id)
    VALUES
      (NEW.alert_key, NEW.last_seen_at, v_recipient, NEW.severity, 'QUEUED', v_request_id)
    ON CONFLICT (alert_key, activated_at) DO NOTHING;
  EXCEPTION WHEN OTHERS THEN
    INSERT INTO genrai_ops.queue_alert_notifications
      (alert_key, activated_at, recipient, severity, status, error)
    VALUES
      (NEW.alert_key, NEW.last_seen_at, v_recipient, NEW.severity, 'FAILED', left(SQLERRM, 1000))
    ON CONFLICT (alert_key, activated_at) DO NOTHING;
  END;

  RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_genrai_queue_alert_email ON genrai_ops.queue_alerts;
CREATE TRIGGER trg_genrai_queue_alert_email
AFTER INSERT OR UPDATE OF resolved_at ON genrai_ops.queue_alerts
FOR EACH ROW
EXECUTE FUNCTION genrai_ops.notify_queue_alert_email();

COMMENT ON TABLE genrai_ops.queue_alert_notifications IS
  'Audit log for one email notification per queue-alert activation; Resend delivery is asynchronous.';
