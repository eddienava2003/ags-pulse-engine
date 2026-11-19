-- GENRAI-INTL-03: deterministic cross-border route correlation and risk scoring.
-- Advisory route intelligence only. No shipment or shipment_telemetry write-back.
CREATE SCHEMA IF NOT EXISTS genrai_ops;
CREATE TABLE IF NOT EXISTS genrai_ops.intl_route_legs (
  event_id text PRIMARY KEY, route_id text NOT NULL, shipment_reference text,
  leg_sequence integer, event_type text NOT NULL, transport_mode text,
  origin_country text, destination_country text, leg_status text, handoff_status text,
  severity text NOT NULL DEFAULT 'LOW', estimated_delay_hours numeric,
  border_queue_hours numeric, port_dwell_hours numeric,
  observed_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT intl_route_legs_nonnegative_metrics CHECK (
    (estimated_delay_hours IS NULL OR estimated_delay_hours >= 0) AND
    (border_queue_hours IS NULL OR border_queue_hours >= 0) AND
    (port_dwell_hours IS NULL OR port_dwell_hours >= 0)
  )
);
CREATE INDEX IF NOT EXISTS idx_intl_route_legs_route_sequence ON genrai_ops.intl_route_legs(route_id, leg_sequence);
CREATE INDEX IF NOT EXISTS idx_intl_route_legs_shipment ON genrai_ops.intl_route_legs(shipment_reference);
CREATE TABLE IF NOT EXISTS genrai_ops.intl_route_assessments (
  route_id text PRIMARY KEY, shipment_reference text,
  total_legs integer NOT NULL DEFAULT 0, sequenced_legs integer NOT NULL DEFAULT 0,
  sequence_gaps jsonb NOT NULL DEFAULT '[]'::jsonb,
  route_risk_score integer NOT NULL DEFAULT 0 CHECK (route_risk_score BETWEEN 0 AND 100),
  risk_band text NOT NULL DEFAULT 'LOW', findings jsonb NOT NULL DEFAULT '[]'::jsonb,
  route_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
  source_event_count integer NOT NULL DEFAULT 0, assessed_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE genrai_ops.intl_route_legs ENABLE ROW LEVEL SECURITY;
ALTER TABLE genrai_ops.intl_route_assessments ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON genrai_ops.intl_route_legs FROM PUBLIC, anon, authenticated;
REVOKE ALL ON genrai_ops.intl_route_assessments FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON genrai_ops.intl_route_legs TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON genrai_ops.intl_route_assessments TO service_role;
CREATE OR REPLACE FUNCTION public.assess_genrai_international_route()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public', 'genrai_ops'
AS $function$
DECLARE
  v_intl jsonb;
  v_route_id text;
  v_shipment_ref text;
  v_leg_sequence integer;
  v_delay numeric;
  v_border numeric;
  v_port numeric;
  v_severity text;
  v_origin text;
  v_destination text;
  v_mode text;
  v_leg_status text;
  v_handoff text;
  v_total integer;
  v_sequenced integer;
  v_min_seq integer;
  v_max_seq integer;
  v_gaps jsonb := '[]'::jsonb;
  v_failed_handoffs integer;
  v_high_legs integer;
  v_medium_legs integer;
  v_max_delay numeric;
  v_max_border numeric;
  v_max_port numeric;
  v_score integer;
  v_band text;
  v_findings jsonb := '[]'::jsonb;
  v_summary jsonb;
BEGIN
  IF NEW.status <> 'ANALYZED' OR NEW.engine_verdict IS NULL OR NEW.event_type NOT IN (
    'CROSS_BORDER_TRANSIT','CUSTOMS_CLEARANCE','CUSTOMS_DOCUMENT_EXCEPTION',
    'PORT_CONGESTION','MULTIMODAL_HANDOFF'
  ) THEN
    RETURN NEW;
  END IF;

  v_intl := COALESCE(NEW.payload -> 'international', '{}'::jsonb);
  IF jsonb_typeof(v_intl) <> 'object' THEN RETURN NEW; END IF;

  v_route_id := NULLIF(left(trim(COALESCE(v_intl ->> 'route_id','')),128),'');
  IF v_route_id IS NULL THEN RETURN NEW; END IF;

  v_shipment_ref := NULLIF(left(trim(COALESCE(v_intl ->> 'shipment_reference','')),128),'');
  v_origin := upper(NULLIF(trim(COALESCE(v_intl ->> 'origin_country','')),''));
  v_destination := upper(NULLIF(trim(COALESCE(v_intl ->> 'destination_country','')),''));
  v_mode := lower(NULLIF(trim(COALESCE(v_intl ->> 'transport_mode','')),''));
  v_leg_status := lower(NULLIF(trim(COALESCE(v_intl ->> 'leg_status','')),''));
  v_handoff := lower(NULLIF(trim(COALESCE(v_intl ->> 'handoff_status','')),''));

  IF COALESCE(v_intl ->> 'leg_sequence','') ~ '^[0-9]{1,6}$' THEN
    v_leg_sequence := (v_intl ->> 'leg_sequence')::integer;
  END IF;
  IF COALESCE(v_intl ->> 'estimated_delay_hours','') ~ '^[0-9]+([.][0-9]+)?$' THEN
    v_delay := LEAST((v_intl ->> 'estimated_delay_hours')::numeric, 1000000);
  END IF;
  IF COALESCE(v_intl ->> 'border_queue_hours','') ~ '^[0-9]+([.][0-9]+)?$' THEN
    v_border := LEAST((v_intl ->> 'border_queue_hours')::numeric, 1000000);
  END IF;
  IF COALESCE(v_intl ->> 'port_dwell_hours','') ~ '^[0-9]+([.][0-9]+)?$' THEN
    v_port := LEAST((v_intl ->> 'port_dwell_hours')::numeric, 1000000);
  END IF;

  v_severity := upper(COALESCE(NEW.engine_verdict ->> 'severity','LOW'));
  IF v_severity NOT IN ('LOW','MEDIUM','HIGH','CRITICAL') THEN v_severity := 'LOW'; END IF;

  INSERT INTO genrai_ops.intl_route_legs (
    event_id, route_id, shipment_reference, leg_sequence, event_type,
    transport_mode, origin_country, destination_country, leg_status,
    handoff_status, severity, estimated_delay_hours, border_queue_hours,
    port_dwell_hours, observed_at, updated_at
  ) VALUES (
    NEW.event_id, v_route_id, v_shipment_ref, v_leg_sequence, NEW.event_type,
    v_mode, v_origin, v_destination, v_leg_status, v_handoff, v_severity,
    v_delay, v_border, v_port, COALESCE(NEW.analyzed_at, NEW.received_at, now()), now()
  )
  ON CONFLICT (event_id) DO UPDATE SET
    route_id = EXCLUDED.route_id,
    shipment_reference = EXCLUDED.shipment_reference,
    leg_sequence = EXCLUDED.leg_sequence,
    event_type = EXCLUDED.event_type,
    transport_mode = EXCLUDED.transport_mode,
    origin_country = EXCLUDED.origin_country,
    destination_country = EXCLUDED.destination_country,
    leg_status = EXCLUDED.leg_status,
    handoff_status = EXCLUDED.handoff_status,
    severity = EXCLUDED.severity,
    estimated_delay_hours = EXCLUDED.estimated_delay_hours,
    border_queue_hours = EXCLUDED.border_queue_hours,
    port_dwell_hours = EXCLUDED.port_dwell_hours,
    observed_at = EXCLUDED.observed_at,
    updated_at = now();

  SELECT count(*)::integer,
         count(leg_sequence)::integer,
         min(leg_sequence),
         max(leg_sequence),
         count(*) FILTER (WHERE severity IN ('HIGH','CRITICAL'))::integer,
         count(*) FILTER (WHERE severity = 'MEDIUM')::integer,
         count(*) FILTER (WHERE handoff_status IN ('missed','failed','not_received'))::integer,
         max(estimated_delay_hours),
         max(border_queue_hours),
         max(port_dwell_hours)
    INTO v_total, v_sequenced, v_min_seq, v_max_seq,
         v_high_legs, v_medium_legs, v_failed_handoffs,
         v_max_delay, v_max_border, v_max_port
  FROM genrai_ops.intl_route_legs
  WHERE route_id = v_route_id;

  IF v_min_seq IS NOT NULL AND v_max_seq - v_min_seq <= 10000 THEN
    SELECT COALESCE(jsonb_agg(gs.n ORDER BY gs.n), '[]'::jsonb)
      INTO v_gaps
    FROM generate_series(v_min_seq, v_max_seq) AS gs(n)
    WHERE NOT EXISTS (
      SELECT 1 FROM genrai_ops.intl_route_legs l
      WHERE l.route_id = v_route_id AND l.leg_sequence = gs.n
    );
  END IF;

  v_score :=
      CASE WHEN v_high_legs > 0 THEN 30 WHEN v_medium_legs > 0 THEN 15 ELSE 0 END
    + CASE WHEN v_failed_handoffs > 0 THEN 35 ELSE 0 END
    + CASE WHEN jsonb_array_length(v_gaps) > 0 THEN 15 ELSE 0 END
    + CASE WHEN v_max_delay >= 24 THEN 20 WHEN v_max_delay >= 8 THEN 10 ELSE 0 END
    + CASE WHEN v_max_border >= 6 THEN 15 WHEN v_max_border >= 3 THEN 8 ELSE 0 END
    + CASE WHEN v_max_port >= 48 THEN 15 WHEN v_max_port >= 24 THEN 8 ELSE 0 END;
  v_score := LEAST(100, v_score);

  v_band := CASE
    WHEN v_score >= 80 THEN 'CRITICAL'
    WHEN v_score >= 55 THEN 'HIGH'
    WHEN v_score >= 25 THEN 'ELEVATED'
    ELSE 'LOW'
  END;

  IF v_failed_handoffs > 0 THEN
    v_findings := v_findings || jsonb_build_array(jsonb_build_object(
      'signal','ROUTE_HANDOFF_FAILURE','severity','HIGH','affected_legs',v_failed_handoffs
    ));
  END IF;
  IF jsonb_array_length(v_gaps) > 0 THEN
    v_findings := v_findings || jsonb_build_array(jsonb_build_object(
      'signal','ROUTE_SEQUENCE_GAP','severity','MEDIUM','missing_leg_sequences',v_gaps
    ));
  END IF;
  IF v_max_delay >= 8 THEN
    v_findings := v_findings || jsonb_build_array(jsonb_build_object(
      'signal','ROUTE_CUMULATIVE_DELAY','severity',CASE WHEN v_max_delay >= 24 THEN 'HIGH' ELSE 'MEDIUM' END,
      'max_reported_leg_delay_hours',v_max_delay
    ));
  END IF;
  IF v_max_border >= 3 THEN
    v_findings := v_findings || jsonb_build_array(jsonb_build_object(
      'signal','ROUTE_BORDER_QUEUE','severity',CASE WHEN v_max_border >= 6 THEN 'HIGH' ELSE 'MEDIUM' END,
      'max_reported_queue_hours',v_max_border
    ));
  END IF;
  IF v_max_port >= 24 THEN
    v_findings := v_findings || jsonb_build_array(jsonb_build_object(
      'signal','ROUTE_PORT_DWELL','severity',CASE WHEN v_max_port >= 48 THEN 'HIGH' ELSE 'MEDIUM' END,
      'max_reported_port_dwell_hours',v_max_port
    ));
  END IF;
  IF v_high_legs > 0 OR v_medium_legs > 0 THEN
    v_findings := v_findings || jsonb_build_array(jsonb_build_object(
      'signal','ROUTE_LEG_RISK','severity',CASE WHEN v_high_legs > 0 THEN 'HIGH' ELSE 'MEDIUM' END,
      'high_or_critical_legs',v_high_legs,'medium_legs',v_medium_legs
    ));
  END IF;

  v_summary := jsonb_build_object(
    'leg_count',v_total,
    'sequenced_leg_count',v_sequenced,
    'first_leg_sequence',v_min_seq,
    'last_leg_sequence',v_max_seq,
    'max_reported_leg_delay_hours',v_max_delay,
    'max_border_queue_hours',v_max_border,
    'max_port_dwell_hours',v_max_port,
    'failed_handoff_count',v_failed_handoffs,
    'route_rule_version','1.0.0'
  );

  INSERT INTO genrai_ops.intl_route_assessments (
    route_id, shipment_reference, total_legs, sequenced_legs,
    sequence_gaps, route_risk_score, risk_band, findings,
    route_summary, source_event_count, assessed_at
  ) VALUES (
    v_route_id, v_shipment_ref, v_total, v_sequenced,
    v_gaps, v_score, v_band, v_findings, v_summary, v_total, now()
  )
  ON CONFLICT (route_id) DO UPDATE SET
    shipment_reference = COALESCE(EXCLUDED.shipment_reference, genrai_ops.intl_route_assessments.shipment_reference),
    total_legs = EXCLUDED.total_legs,
    sequenced_legs = EXCLUDED.sequenced_legs,
    sequence_gaps = EXCLUDED.sequence_gaps,
    route_risk_score = EXCLUDED.route_risk_score,
    risk_band = EXCLUDED.risk_band,
    findings = EXCLUDED.findings,
    route_summary = EXCLUDED.route_summary,
    source_event_count = EXCLUDED.source_event_count,
    assessed_at = now();

  RETURN NEW;
END;
$function$

DROP TRIGGER IF EXISTS trg_genrai_intl_route_assessment ON public.genrai_telemetry_events;
CREATE TRIGGER trg_genrai_intl_route_assessment
AFTER UPDATE OF status ON public.genrai_telemetry_events
FOR EACH ROW
WHEN (NEW.status = 'ANALYZED' AND NEW.event_type IN ('CROSS_BORDER_TRANSIT','CUSTOMS_CLEARANCE','CUSTOMS_DOCUMENT_EXCEPTION','PORT_CONGESTION','MULTIMODAL_HANDOFF'))
EXECUTE FUNCTION public.assess_genrai_international_route();
COMMENT ON TABLE genrai_ops.intl_route_legs IS 'Normalized event-derived international route legs for GENRAI-INTL-03; advisory only.';
COMMENT ON TABLE genrai_ops.intl_route_assessments IS 'Deterministic route-level risk assessments derived from correlated international legs; advisory only.';
