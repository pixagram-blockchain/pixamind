-- Pixamind runtime: shared helpers. Reinstalled on every indexer start.

CREATE OR REPLACE FUNCTION pixamind.set_setting(_name TEXT, _value TEXT)
RETURNS VOID LANGUAGE sql VOLATILE AS
$$
  INSERT INTO pixamind.settings (name, value, updated_at) VALUES (_name, _value, now())
  ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at;
$$;

CREATE OR REPLACE FUNCTION pixamind.get_setting(_name TEXT, _default TEXT DEFAULT NULL)
RETURNS TEXT LANGUAGE sql STABLE AS
$$ SELECT COALESCE((SELECT value FROM pixamind.settings WHERE name = _name), _default) $$;

-- Record a failed projection step (called from EXCEPTION handlers, so it must not raise itself).
CREATE OR REPLACE FUNCTION pixamind.record_failure(_projection TEXT, _first INT, _last INT, _sqlstate TEXT, _error TEXT)
RETURNS VOID LANGUAGE plpgsql VOLATILE AS
$$
BEGIN
  INSERT INTO pixamind.projection_failures (projection, first_block, last_block, sqlstate, error)
  VALUES (_projection, _first, _last, _sqlstate, left(_error, 2000));
  RAISE WARNING 'pixamind: % failed for blocks %..%: % (%)', _projection, _first, _last, _error, _sqlstate;
EXCEPTION WHEN OTHERS THEN
  RAISE WARNING 'pixamind: could not record % failure for blocks %..%: %', _projection, _first, _last, SQLERRM;
END
$$;

-- A later successful run of a projection over a range resolves earlier failures inside it.
CREATE OR REPLACE FUNCTION pixamind.resolve_failures(_projection TEXT, _first INT, _last INT)
RETURNS VOID LANGUAGE sql VOLATILE AS
$$
  UPDATE pixamind.projection_failures SET resolved_at = clock_timestamp()
  WHERE projection = _projection AND resolved_at IS NULL AND first_block >= _first AND last_block <= _last;
$$;

-- Event cursor: "<op_id>-<seq>". A string, because op ids exceed 2^53 and
-- JavaScript clients would silently round a JSON number.
CREATE OR REPLACE FUNCTION pixamind.event_cursor(_op_id BIGINT, _seq SMALLINT)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$ SELECT _op_id::TEXT || '-' || _seq::TEXT $$;

-- Parse a cursor; NULL for NULL/empty input, an exception for a malformed one.
CREATE OR REPLACE FUNCTION pixamind.parse_event_cursor(_cursor TEXT, OUT op_id BIGINT, OUT seq SMALLINT)
LANGUAGE plpgsql IMMUTABLE AS
$$
BEGIN
  IF _cursor IS NULL OR _cursor = '' THEN
    RETURN;
  END IF;
  IF _cursor ~ '^[0-9]{1,19}-[0-9]{1,5}$' THEN   -- checked first: the casts below need digits
    IF split_part(_cursor, '-', 1)::NUMERIC <= 9223372036854775807 AND split_part(_cursor, '-', 2)::INT <= 32767 THEN
      op_id := split_part(_cursor, '-', 1)::BIGINT;
      seq := split_part(_cursor, '-', 2)::SMALLINT;
      RETURN;
    END IF;
  END IF;
  RAISE EXCEPTION '%', hivemind_postgrest_utilities.raise_parameter_validation_exception('invalid cursor: ' || left(_cursor, 64));
END
$$;

-- "1.000 PXS"-style amount for the chain's stable token.
CREATE OR REPLACE FUNCTION pixamind.stable_amount_text(_amount NUMERIC)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS
$$ SELECT to_char(COALESCE(_amount, 0), 'FM999999999990.000') || ' {{HBD_SYMBOL}}' $$;

-- Decimal value of a NAI amount object {"amount": "1000", "precision": 3, "nai": "@@000000013"}.
CREATE OR REPLACE FUNCTION pixamind.nai_value(_amount JSONB)
RETURNS NUMERIC LANGUAGE sql IMMUTABLE AS
$$
  SELECT CASE
    WHEN jsonb_typeof(_amount) = 'object' AND (_amount->>'amount') ~ '^[0-9]{1,20}$'
      THEN (_amount->>'amount')::NUMERIC / (10::NUMERIC ^ COALESCE((_amount->>'precision')::INT, 3))
  END
$$;

-- Symbol of a NAI amount object, using this chain's symbols.
CREATE OR REPLACE FUNCTION pixamind.nai_symbol(_amount JSONB)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS
$$
  SELECT CASE _amount->>'nai'
    WHEN '@@000000013' THEN '{{HBD_SYMBOL}}'
    WHEN '@@000000021' THEN '{{HIVE_SYMBOL}}'
    WHEN '@@000000037' THEN '{{VESTS_SYMBOL}}'
  END
$$;

-- "1.234 PIXA" text for a NAI amount object (NULL when it cannot be read).
CREATE OR REPLACE FUNCTION pixamind.nai_text(_amount JSONB)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS
$$
  SELECT to_char(pixamind.nai_value(_amount),
                 'FM999999999990.' || repeat('0', COALESCE((_amount->>'precision')::INT, 3)))
         || ' ' || pixamind.nai_symbol(_amount)
  WHERE pixamind.nai_value(_amount) IS NOT NULL AND pixamind.nai_symbol(_amount) IS NOT NULL
$$;

-- Parse JSON text that may be invalid (json_metadata, custom_json payloads): NULL instead of an error.
CREATE OR REPLACE FUNCTION pixamind.try_jsonb(_text TEXT)
RETURNS JSONB LANGUAGE plpgsql IMMUTABLE AS
$$
BEGIN
  IF _text IS NULL OR _text = '' THEN
    RETURN NULL;
  END IF;
  RETURN _text::JSONB;
EXCEPTION WHEN OTHERS THEN
  RETURN NULL;
END
$$;

-- An account name as the chain allows it (3-16 chars), else NULL.
CREATE OR REPLACE FUNCTION pixamind.account_or_null(_name TEXT)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS
$$ SELECT CASE WHEN _name ~ '^[a-z][a-z0-9.-]{2,15}$' THEN _name END $$;
