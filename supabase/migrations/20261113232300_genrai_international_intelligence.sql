-- GENRAI-1 international logistics intelligence layer.
-- This trigger enriches analyzed synthetic/partner international events only.
-- It never writes to shipments or shipment_telemetry.

CREATE OR REPLACE FUNCTION public.enrich_genrai_international_verdict()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'pg_catalog', 'public'
AS $function$
DECLARE
  v_intl jsonb;
  v_origin text;
  v_destination text;
  v_customs text;
  v_docs text;
  v_handoff text;
  v_mode text;
  v_border_queue numeric;
  v_port_dwell numeric;
  v_estimated_delay numeric;
  v_findings jsonb;
  v_actions jsonb;
  v_severity text := 'LOW';
  v_verdict text := 'NORMAL';
  v_context jsonb;
BEGIN
  IF NEW.status <> 'ANALYZED'
     OR NEW.engine_verdict IS NULL
     OR NEW.event_type NOT IN (
       'CROSS_BORDER_TRANSIT',
       'CUSTOMS_CLEARANCE',
       'CUSTOMS_DOCUMENT_EXCEPTION',
       'PORT_CONGESTION',
       'MULTIMODAL_HANDOFF'
     )
     OR NEW.engine_verdict ? 'international_rule_version' THEN
    RETURN NEW;
  END IF;

  v_intl := COALESCE(NEW.payload -> 'international', '{}'::jsonb);
  IF jsonb_typeof(v_intl) <> 'object' THEN
    v_intl := '{}'::jsonb;
  END IF;

  v_origin := upper(trim(COALESCE(v_intl ->> 'origin_country', '')));
  v_destination := upper(trim(COALESCE(v_intl ->> 'destination_country', '')));
  v_customs := lower(trim(COALESCE(v_intl ->> 'customs_status', '')));
  v_docs := lower(trim(COALESCE(v_intl ->> 'documentation_status', '')));
  v_handoff := lower(trim(COALESCE(v_intl ->> 'handoff_status', '')));
  v_mode := lower(trim(COALESCE(v_intl ->> 'transport_mode', '')));

  IF COALESCE(v_intl ->> 'border_queue_hours', '') ~ '^[0-9]+([.][0-9]+)?$' THEN
    v_border_queue := (v_intl ->> 'border_queue_hours')::numeric;
  END IF;
  IF COALESCE(v_intl ->> 'port_dwell_hours', '') ~ '^[0-9]+([.][0-9]+)?$' THEN
    v_port_dwell := (v_intl ->> 'port_dwell_hours')::numeric;
  END IF;
  IF COALESCE(v_intl ->> 'estimated_delay_hours', '') ~ '^[0-9]+([.][0-9]+)?$' THEN
    v_estimated_delay := (v_intl ->> 'estimated_delay_hours')::numeric;
  END IF;

  v_findings := COALESCE(NEW.engine_verdict -> 'findings', '[]'::jsonb);
  IF jsonb_typeof(v_findings) <> 'array' THEN v_findings := '[]'::jsonb; END IF;
  v_actions := COALESCE(NEW.engine_verdict -> 'recommended_actions', '[]'::jsonb);
  IF jsonb_typeof(v_actions) <> 'array' THEN v_actions := '[]'::jsonb; END IF;

  IF v_origin !~ '^[A-Z]{2}$' OR v_destination !~ '^[A-Z]{2}$' THEN
    v_findings := v_findings || jsonb_build_array(jsonb_build_object(
      'signal', 'INCOMPLETE_CROSS_BORDER_CONTEXT',
      'severity', 'MEDIUM',
      'detail', 'origin_country and destination_country must be ISO 3166-1 alpha-2 codes'
    ));
    v_actions := v_actions || jsonb_build_array('Validate origin and destination country codes before operational use.');
    v_severity := 'MEDIUM';
  END IF;

  IF v_customs IN ('hold', 'held', 'inspection_required', 'detained') THEN
    v_findings := v_findings || jsonb_build_array(jsonb_build_object(
      'signal', 'CUSTOMS_HOLD', 'value', v_customs, 'severity', 'HIGH'
    ));
    v_actions := v_actions || jsonb_build_array('Escalate the customs hold and verify the release requirements with the responsible broker or authority.');
    v_severity := 'HIGH';
  END IF;

  IF v_docs IN ('missing', 'mismatch', 'invalid', 'incomplete', 'rejected') THEN
    v_findings := v_findings || jsonb_build_array(jsonb_build_object(
      'signal', 'CUSTOMS_DOCUMENT_EXCEPTION', 'value', v_docs, 'severity', 'HIGH'
    ));
    v_actions := v_actions || jsonb_build_array('Reconcile commercial invoice, packing list, and applicable customs documentation before release.');
    v_severity := 'HIGH';
  END IF;

  IF v_handoff IN ('missed', 'failed', 'not_received') THEN
    v_findings := v_findings || jsonb_build_array(jsonb_build_object(
      'signal', 'MULTIMODAL_HANDOFF_FAILURE', 'value', v_handoff, 'severity', 'HIGH'
    ));
    v_actions := v_actions || jsonb_build_array('Contact both transport operators and confirm custody, location, and the recovery connection.');
    v_severity := 'HIGH';
  END IF;

  IF v_border_queue >= 6 THEN
    v_findings := v_findings || jsonb_build_array(jsonb_build_object(
      'signal', 'BORDER_QUEUE_DELAY', 'value', v_border_queue, 'threshold', 6, 'severity', 'HIGH'
    ));
    v_actions := v_actions || jsonb_build_array('Assess alternate crossing capacity and update the cross-border recovery plan.');
    v_severity := 'HIGH';
  ELSIF v_border_queue >= 3 THEN
    v_findings := v_findings || jsonb_build_array(jsonb_build_object(
      'signal', 'BORDER_QUEUE_DELAY', 'value', v_border_queue, 'threshold', 3, 'severity', 'MEDIUM'
    ));
    v_actions := v_actions || jsonb_build_array('Monitor border queue conditions and reassess the crossing appointment.');
    IF v_severity = 'LOW' THEN v_severity := 'MEDIUM'; END IF;
  END IF;

  IF v_port_dwell >= 48 THEN
    v_findings := v_findings || jsonb_build_array(jsonb_build_object(
      'signal', 'PORT_CONGESTION_DWELL', 'value', v_port_dwell, 'threshold', 48, 'severity', 'HIGH'
    ));
    v_actions := v_actions || jsonb_build_array('Escalate terminal dwell and verify demurrage, detention, and next available movement options.');
    v_severity := 'HIGH';
  ELSIF v_port_dwell >= 24 THEN
    v_findings := v_findings || jsonb_build_array(jsonb_build_object(
      'signal', 'PORT_CONGESTION_DWELL', 'value', v_port_dwell, 'threshold', 24, 'severity', 'MEDIUM'
    ));
    v_actions := v_actions || jsonb_build_array('Monitor terminal dwell and confirm the container release and pickup plan.');
    IF v_severity = 'LOW' THEN v_severity := 'MEDIUM'; END IF;
  END IF;

  IF v_estimated_delay >= 24 THEN
    v_findings := v_findings || jsonb_build_array(jsonb_build_object(
      'signal', 'INTERNATIONAL_TRANSIT_DELAY', 'value', v_estimated_delay, 'threshold', 24, 'severity', 'HIGH'
    ));
    v_actions := v_actions || jsonb_build_array('Recalculate downstream milestones and notify affected logistics stakeholders.');
    v_severity := 'HIGH';
  ELSIF v_estimated_delay >= 8 THEN
    v_findings := v_findings || jsonb_build_array(jsonb_build_object(
      'signal', 'INTERNATIONAL_TRANSIT_DELAY', 'value', v_estimated_delay, 'threshold', 8, 'severity', 'MEDIUM'
    ));
    v_actions := v_actions || jsonb_build_array('Reassess the international ETA and monitor connecting transport capacity.');
    IF v_severity = 'LOW' THEN v_severity := 'MEDIUM'; END IF;
  END IF;

  IF v_severity = 'HIGH' THEN
    v_verdict := 'HIGH_RISK';
  ELSIF v_severity = 'MEDIUM' THEN
    v_verdict := 'ELEVATED_RISK';
  END IF;

  v_context := jsonb_build_object(
    'origin_country', NULLIF(v_origin, ''),
    'destination_country', NULLIF(v_destination, ''),
    'transport_mode', NULLIF(v_mode, ''),
    'customs_status', NULLIF(v_customs, ''),
    'documentation_status', NULLIF(v_docs, ''),
    'handoff_status', NULLIF(v_handoff, ''),
    'border_queue_hours', v_border_queue,
    'port_dwell_hours', v_port_dwell,
    'estimated_delay_hours', v_estimated_delay
  );

  UPDATE public.genrai_telemetry_events
  SET engine_verdict = NEW.engine_verdict || jsonb_build_object(
        'verdict', v_verdict,
        'severity', v_severity,
        'findings', v_findings,
        'recommended_actions', v_actions,
        'international_context', v_context,
        'international_rule_version', '1.0.0',
        'international_processed_at', now()
      )
  WHERE event_id = NEW.event_id;

  RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_genrai_international_enrich ON public.genrai_telemetry_events;
CREATE TRIGGER trg_genrai_international_enrich
AFTER UPDATE OF status ON public.genrai_telemetry_events
FOR EACH ROW
WHEN (
  NEW.status = 'ANALYZED'
  AND NEW.event_type IN (
    'CROSS_BORDER_TRANSIT',
    'CUSTOMS_CLEARANCE',
    'CUSTOMS_DOCUMENT_EXCEPTION',
    'PORT_CONGESTION',
    'MULTIMODAL_HANDOFF'
  )
)
EXECUTE FUNCTION public.enrich_genrai_international_verdict();
