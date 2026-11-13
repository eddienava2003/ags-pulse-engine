-- GENRAI-1 trade-data consistency checks (synthetic/partner event contract).
-- Deterministic format and completeness checks only; not customs-law validation.
CREATE OR REPLACE FUNCTION public.enrich_genrai_trade_verdict()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'pg_catalog', 'public'
AS $function$
DECLARE
  v_payload jsonb;
  v_trade jsonb;
  v_verdict jsonb;
  v_findings jsonb;
  v_actions jsonb;
  v_hs text;
  v_incoterm text;
  v_incoterm_year text;
  v_required jsonb;
  v_provided jsonb;
  v_missing jsonb := '[]'::jsonb;
  v_doc text;
  v_responsibility text;
  v_severity text;
BEGIN
  IF NEW.status <> 'ANALYZED'
     OR NEW.event_type NOT IN ('CROSS_BORDER_TRANSIT','CUSTOMS_CLEARANCE','CUSTOMS_DOCUMENT_EXCEPTION','PORT_CONGESTION','MULTIMODAL_HANDOFF') THEN
    RETURN NEW;
  END IF;

  SELECT COALESCE(engine_verdict, '{}'::jsonb), COALESCE(payload, '{}'::jsonb)
    INTO v_verdict, v_payload
  FROM public.genrai_telemetry_events
  WHERE event_id = NEW.event_id;

  IF v_verdict ? 'trade_rule_version' THEN RETURN NEW; END IF;

  v_trade := COALESCE(v_payload -> 'international' -> 'trade', '{}'::jsonb);
  IF jsonb_typeof(v_trade) <> 'object' THEN v_trade := '{}'::jsonb; END IF;

  v_findings := COALESCE(v_verdict -> 'findings', '[]'::jsonb);
  IF jsonb_typeof(v_findings) <> 'array' THEN v_findings := '[]'::jsonb; END IF;
  v_actions := COALESCE(v_verdict -> 'recommended_actions', '[]'::jsonb);
  IF jsonb_typeof(v_actions) <> 'array' THEN v_actions := '[]'::jsonb; END IF;
  v_severity := upper(COALESCE(v_verdict ->> 'severity', 'LOW'));

  v_hs := regexp_replace(COALESCE(v_trade ->> 'hs_code', ''), '[ .-]', '', 'g');
  IF v_hs <> '' AND v_hs !~ '^[0-9]{6}([0-9]{2})?([0-9]{2})?$' THEN
    v_findings := v_findings || jsonb_build_array(jsonb_build_object(
      'signal','HS_CODE_FORMAT_INVALID','severity','MEDIUM',
      'detail','HS code must contain 6, 8, or 10 digits after removing spaces, periods, and hyphens.'
    ));
    v_actions := v_actions || jsonb_build_array('Validate the commodity code against the applicable official tariff schedule.');
    IF v_severity NOT IN ('HIGH','CRITICAL') THEN v_severity := 'MEDIUM'; END IF;
  END IF;

  v_incoterm := upper(trim(COALESCE(v_trade ->> 'incoterm', '')));
  v_incoterm_year := trim(COALESCE(v_trade ->> 'incoterm_version', ''));
  v_responsibility := CASE v_incoterm
    WHEN 'EXW' THEN 'Seller makes goods available; buyer generally arranges main carriage and export/import formalities.'
    WHEN 'FCA' THEN 'Seller delivers to the buyer-nominated carrier at the named place; allocation depends on delivery point.'
    WHEN 'CPT' THEN 'Seller pays carriage to the named destination; risk transfers on delivery to the first carrier.'
    WHEN 'CIP' THEN 'Seller pays carriage and specified insurance to destination; risk transfers on delivery to the first carrier.'
    WHEN 'DAP' THEN 'Seller arranges carriage to the named destination; buyer handles import clearance and unloading.'
    WHEN 'DPU' THEN 'Seller arranges carriage and unloading at the named destination; buyer handles import clearance.'
    WHEN 'DDP' THEN 'Seller bears delivery responsibility including import clearance and applicable duties/taxes.'
    WHEN 'FAS' THEN 'Seller delivers alongside the vessel at the named port; buyer arranges loading and main carriage.'
    WHEN 'FOB' THEN 'Seller delivers goods on board the vessel at the named port; buyer arranges main carriage.'
    WHEN 'CFR' THEN 'Seller pays cost and freight to destination port; risk transfers when goods are on board.'
    WHEN 'CIF' THEN 'Seller pays cost, freight, and specified insurance to destination port; risk transfers when goods are on board.'
    ELSE NULL
  END;

  IF v_incoterm <> '' AND v_responsibility IS NULL THEN
    v_findings := v_findings || jsonb_build_array(jsonb_build_object(
      'signal','INCOTERM_CODE_UNRECOGNIZED','value',v_incoterm,'severity','MEDIUM'
    ));
    v_actions := v_actions || jsonb_build_array('Confirm the Incoterms rule code and named place with the trading parties.');
    IF v_severity NOT IN ('HIGH','CRITICAL') THEN v_severity := 'MEDIUM'; END IF;
  ELSIF v_incoterm <> '' AND v_incoterm_year NOT IN ('2020') THEN
    v_findings := v_findings || jsonb_build_array(jsonb_build_object(
      'signal','INCOTERM_VERSION_UNCONFIRMED','value',v_incoterm_year,'severity','MEDIUM',
      'detail','This ruleset currently recognizes Incoterms 2020 only.'
    ));
    v_actions := v_actions || jsonb_build_array('Confirm the agreed Incoterms edition and named place in the sales contract.');
    IF v_severity NOT IN ('HIGH','CRITICAL') THEN v_severity := 'MEDIUM'; END IF;
  END IF;

  v_required := v_trade -> 'required_documents';
  v_provided := v_trade -> 'provided_documents';
  IF jsonb_typeof(v_required) = 'array' AND jsonb_typeof(v_provided) = 'array' THEN
    FOR v_doc IN SELECT jsonb_array_elements_text(v_required)
    LOOP
      IF NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements_text(v_provided) p(value)
        WHERE lower(trim(p.value)) = lower(trim(v_doc))
      ) THEN
        v_missing := v_missing || jsonb_build_array(v_doc);
      END IF;
    END LOOP;
    IF jsonb_array_length(v_missing) > 0 THEN
      v_findings := v_findings || jsonb_build_array(jsonb_build_object(
        'signal','TRADE_DOCUMENT_SET_INCOMPLETE','missing_documents',v_missing,'severity','HIGH'
      ));
      v_actions := v_actions || jsonb_build_array('Obtain or reconcile the listed trade documents before the relevant clearance milestone.');
      v_severity := 'HIGH';
    END IF;
  ELSIF v_required IS NOT NULL OR v_provided IS NOT NULL THEN
    v_findings := v_findings || jsonb_build_array(jsonb_build_object(
      'signal','TRADE_DOCUMENT_LIST_INVALID','severity','MEDIUM',
      'detail','required_documents and provided_documents must both be JSON arrays for set comparison.'
    ));
    v_actions := v_actions || jsonb_build_array('Normalize required_documents and provided_documents as arrays of document names.');
    IF v_severity NOT IN ('HIGH','CRITICAL') THEN v_severity := 'MEDIUM'; END IF;
  END IF;

  UPDATE public.genrai_telemetry_events
  SET engine_verdict = v_verdict || jsonb_build_object(
    'severity',v_severity,
    'verdict',CASE WHEN v_severity IN ('HIGH','CRITICAL') THEN 'HIGH_RISK'
                   WHEN v_severity = 'MEDIUM' THEN 'ELEVATED_RISK'
                   ELSE COALESCE(v_verdict ->> 'verdict','NORMAL') END,
    'findings',v_findings,
    'recommended_actions',v_actions,
    'trade_context',jsonb_build_object(
      'hs_code_normalized',NULLIF(v_hs,''),
      'incoterm',NULLIF(v_incoterm,''),
      'incoterm_version',NULLIF(v_incoterm_year,''),
      'incoterm_responsibility_note',v_responsibility,
      'missing_documents',v_missing
    ),
    'trade_rule_version','1.0.0',
    'trade_processed_at',now()
  )
  WHERE event_id = NEW.event_id;

  RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_genrai_trade_enrich ON public.genrai_telemetry_events;
CREATE TRIGGER trg_genrai_trade_enrich
AFTER UPDATE OF status ON public.genrai_telemetry_events
FOR EACH ROW
WHEN (
  NEW.status = 'ANALYZED'
  AND NEW.event_type IN ('CROSS_BORDER_TRANSIT','CUSTOMS_CLEARANCE','CUSTOMS_DOCUMENT_EXCEPTION','PORT_CONGESTION','MULTIMODAL_HANDOFF')
)
EXECUTE FUNCTION public.enrich_genrai_trade_verdict();
