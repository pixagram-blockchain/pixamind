-- Pixamind runtime: sync telemetry.
-- One row per massive-sync batch or live block, for pixamind_api.get_metrics
-- and the Systemic Control Tower. Kept for 7 days.

-- Returns the block-to-record latency in seconds (live mode), else NULL.
DROP FUNCTION IF EXISTS pixamind.record_batch(TEXT, INT, INT, REAL, JSONB);
CREATE FUNCTION pixamind.record_batch(
    _mode TEXT, _first INT, _last INT, _seconds REAL, _phases JSONB DEFAULT '{}'
)
RETURNS REAL LANGUAGE plpgsql VOLATILE AS
$$
DECLARE
  _block_time TIMESTAMP;
  _ops INT;
  _latency REAL;
BEGIN
  -- hivemind's staging table still holds the operations of this batch
  SELECT count(*) INTO _ops FROM hivemind_app._ops_staging;
  SELECT created_at INTO _block_time FROM hivemind_app.blocks_view WHERE num = _last;

  IF _mode = 'live' AND _block_time IS NOT NULL THEN
    _latency := EXTRACT(EPOCH FROM ((clock_timestamp() AT TIME ZONE 'UTC') - _block_time))::REAL;
  END IF;

  INSERT INTO pixamind.sync_batches (mode, first_block, last_block, block_time, ops, seconds, latency_seconds, phases)
  VALUES (_mode, _first, _last, _block_time, COALESCE(_ops, 0), _seconds, _latency, COALESCE(_phases, '{}'::JSONB));

  IF _last % 200 = 0 THEN
    DELETE FROM pixamind.sync_batches WHERE recorded_at < clock_timestamp() - INTERVAL '7 days';
  END IF;
  RETURN _latency;
END
$$;
