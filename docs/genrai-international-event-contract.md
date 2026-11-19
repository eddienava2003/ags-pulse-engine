# GENRAI-1 International Event Ingestion Contract

The authenticated `POST /api/v1/telemetry-event` endpoint accepts international logistics events through the existing event envelope. International route correlation is optional for compatibility with standalone international exception events.

## Event envelope

```json
{
  "event_id": "partner-event-unique-id",
  "source_system": "partner-or-AGS-Pulse",
  "event_type": "CROSS_BORDER_TRANSIT",
  "payload": {
    "international": {
      "route_id": "route-2026-00041",
      "shipment_reference": "partner-shipment-reference",
      "leg_sequence": 1,
      "origin_country": "US",
      "destination_country": "MX",
      "transport_mode": "truck",
      "leg_status": "in_transit",
      "handoff_status": "completed",
      "estimated_delay_hours": 0,
      "border_queue_hours": 0,
      "port_dwell_hours": 0
    }
  }
}
```

## Route-correlation rules

- Supported event types: `CROSS_BORDER_TRANSIT`, `CUSTOMS_CLEARANCE`, `CUSTOMS_DOCUMENT_EXCEPTION`, `PORT_CONGESTION`, and `MULTIMODAL_HANDOFF`.
- Use the same `international.route_id` for every event belonging to one end-to-end route.
- `international.leg_sequence` is a required positive integer (1–999999) whenever either route-correlation field is supplied. Sequences should be contiguous and ordered across the route.
- `international.route_id` is a non-empty string of at most 128 characters. `shipment_reference` is optional and at most 128 characters.
- Each physical route leg is represented by one event. Use the event's `event_id` as the idempotency key; replays with identical content return the existing result, while reuse with different content is rejected.
- Country codes should use ISO 3166-1 alpha-2. Transport mode, leg status, and handoff status are descriptive partner values; route risk rules evaluate the known status signals.
- International events are analyzed and correlated into `genrai_ops.intl_route_legs` and `genrai_ops.intl_route_assessments`. Route assessments are advisory. They do not write to `shipments` or `shipment_telemetry`.
- Standalone international events without `route_id` and `leg_sequence` remain supported, but are not included in route-level correlation.

## Example multi-leg route

A single route can contain truck (US→MX), ocean (MX→ES), and rail (ES→DE) legs. All three events share one `route_id`, use sequences 1, 2, and 3, and use unique `event_id` values. The route engine stores each normalized leg and recalculates the route assessment after each event is analyzed.

## Validation

The API rejects partial route correlation (only route_id or only leg_sequence), invalid route IDs, non-integer/zero/out-of-range leg sequences, and invalid shipment-reference types/lengths with HTTP 400. The existing event-id idempotency behavior remains unchanged.
