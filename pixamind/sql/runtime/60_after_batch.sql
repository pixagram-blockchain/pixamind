-- Pixamind runtime: the per-batch entry point called by the indexer.
--
-- Called once per massive-sync batch (own transaction, after hivemind's phases)
-- and once per live block (inside the block's transaction, before COMMIT), so
-- Pixamind's projections commit atomically with hivemind's. Each step runs in
-- its own subtransaction: a failure is recorded in pixamind.projection_failures
-- and the batch carries on. Pixamind extensions never stop the social indexer.
--
-- _options (all optional):
--   {"promotions": true, "events": false, "events_min_block": 0,
--    "events_keep_blocks": 864000, "telemetry": true}
-- Returns a summary, e.g. {"promotions": 1, "events": 42, "failures": 0}.

CREATE OR REPLACE FUNCTION pixamind.after_batch(
    _mode TEXT, _first INT, _last INT, _seconds REAL, _phases JSONB, _options JSONB DEFAULT '{}'
)
RETURNS JSONB LANGUAGE plpgsql VOLATILE AS
$$
DECLARE
  _summary JSONB := '{}'::JSONB;
  _failures INT := 0;
  _n INT;
  _events BOOLEAN := COALESCE((_options->>'events')::BOOLEAN, FALSE);
  _promotions BOOLEAN := COALESCE((_options->>'promotions')::BOOLEAN, TRUE);
  _telemetry BOOLEAN := COALESCE((_options->>'telemetry')::BOOLEAN, TRUE);
  _min_block INT := COALESCE((_options->>'events_min_block')::INT, 0);
  _keep_blocks INT := COALESCE((_options->>'events_keep_blocks')::INT, 0);
BEGIN
  IF _promotions THEN
    BEGIN
      _n := pixamind.process_promotions(_first, _last);
      _summary := _summary || jsonb_build_object('promotions', _n);
      PERFORM pixamind.resolve_failures('promotions', _first, _last);
    EXCEPTION WHEN OTHERS THEN
      PERFORM pixamind.record_failure('promotions', _first, _last, SQLSTATE, SQLERRM);
      _failures := _failures + 1;
    END;
  END IF;

  IF _events THEN
    BEGIN
      _n := pixamind.emit_events(_first, _last, _min_block, 'staging');
      IF _promotions THEN
        _n := _n + pixamind.emit_promotion_events(_first, _last, _min_block);
      END IF;
      _summary := _summary || jsonb_build_object('events', _n);
      PERFORM pixamind.resolve_failures('events', _first, _last);
      -- prune once every 1200 blocks (an hour of 3-second blocks)
      IF _keep_blocks > 0 AND (_last / 1200) > ((_first - 1) / 1200) THEN
        _summary := _summary || jsonb_build_object('events_pruned', pixamind.prune_events(_keep_blocks));
      END IF;
    EXCEPTION WHEN OTHERS THEN
      PERFORM pixamind.record_failure('events', _first, _last, SQLSTATE, SQLERRM);
      _failures := _failures + 1;
    END;
  END IF;

  IF _telemetry THEN
    BEGIN
      _summary := _summary || jsonb_strip_nulls(jsonb_build_object(
        'latency_seconds', pixamind.record_batch(_mode, _first, _last, _seconds, _phases)));
    EXCEPTION WHEN OTHERS THEN
      RAISE WARNING 'pixamind: telemetry not recorded for blocks %..%: %', _first, _last, SQLERRM;
    END;
  END IF;

  RETURN _summary || jsonb_build_object('failures', _failures);
END
$$;
