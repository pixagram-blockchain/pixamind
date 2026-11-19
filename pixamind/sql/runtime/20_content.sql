-- Pixamind runtime: content metadata for Pixagram posts.
--
-- Pixagram artwork posts carry the picture in the post body as base64 (WebP,
-- sometimes PNG) and describe it in json_metadata:
--   {"app": "pixagram/3.0.2", "format": "image", "tags": [...], "description": "...",
--    "nsfw": false, "oc": true, "ai": "", "license": {"type": "PIXA_LICENSE", ...}}
-- Everything below is read from chain data alone, so it is deterministic and
-- identical on every node. Image dimensions come from the file header, which
-- sits in the first few dozen bytes: no image is ever decoded.

-- Header facts of a base64 image body (raw base64 or a data: URI).
-- Returns NULL when the body is not a recognised PNG, GIF or WebP file.
CREATE OR REPLACE FUNCTION pixamind.image_info(_body_head TEXT, _body_length INT DEFAULT NULL)
RETURNS JSONB LANGUAGE plpgsql IMMUTABLE AS
$$
DECLARE
  _b64 TEXT;
  _prefix_len INT := 0;
  _h BYTEA;
  _mime TEXT;
  _w BIGINT;
  _h_px BIGINT;
  _animated BOOLEAN;
  _alpha BOOLEAN;
  _bits BIGINT;
  _chunk TEXT;
  _flags INT;
  _bytes BIGINT;
BEGIN
  IF _body_head IS NULL THEN
    RETURN NULL;
  END IF;
  _b64 := ltrim(_body_head);
  IF left(_b64, 5) = 'data:' THEN
    _prefix_len := strpos(_b64, ',');
    IF _prefix_len = 0 THEN
      RETURN NULL;
    END IF;
    _b64 := substr(_b64, _prefix_len + 1);
  END IF;
  _b64 := left(regexp_replace(_b64, '\s', '', 'g'), 64);
  IF _b64 !~ '^[A-Za-z0-9+/]{64}$' THEN
    RETURN NULL;
  END IF;
  _h := decode(_b64, 'base64');   -- 48 bytes: enough for every header below

  IF substr(_h, 1, 8) = '\x89504e470d0a1a0a'::BYTEA AND substr(_h, 13, 4) = 'IHDR'::BYTEA THEN
    _mime := 'image/png';
    _w := (get_byte(_h, 16)::BIGINT << 24) | (get_byte(_h, 17) << 16) | (get_byte(_h, 18) << 8) | get_byte(_h, 19);
    _h_px := (get_byte(_h, 20)::BIGINT << 24) | (get_byte(_h, 21) << 16) | (get_byte(_h, 22) << 8) | get_byte(_h, 23);
    _alpha := get_byte(_h, 25) IN (4, 6);                 -- grey+alpha, RGBA (tRNS not visible here)
    _animated := substr(_h, 38, 4) = 'acTL'::BYTEA;       -- APNG control chunk right after IHDR
  ELSIF substr(_h, 1, 6) IN ('GIF87a'::BYTEA, 'GIF89a'::BYTEA) THEN
    _mime := 'image/gif';
    _w := get_byte(_h, 6) | (get_byte(_h, 7) << 8);
    _h_px := get_byte(_h, 8) | (get_byte(_h, 9) << 8);
  ELSIF substr(_h, 1, 4) = 'RIFF'::BYTEA AND substr(_h, 9, 4) = 'WEBP'::BYTEA THEN
    _mime := 'image/webp';
    _chunk := convert_from(substr(_h, 13, 4), 'SQL_ASCII');
    IF _chunk = 'VP8 ' THEN                                -- lossy
      IF substr(_h, 24, 3) <> '\x9d012a'::BYTEA THEN
        RETURN NULL;
      END IF;
      _w := (get_byte(_h, 26) | (get_byte(_h, 27) << 8)) & 16383;
      _h_px := (get_byte(_h, 28) | (get_byte(_h, 29) << 8)) & 16383;
      _animated := FALSE;
      _alpha := FALSE;
    ELSIF _chunk = 'VP8L' THEN                             -- lossless
      IF get_byte(_h, 20) <> 47 THEN
        RETURN NULL;
      END IF;
      _bits := get_byte(_h, 21)::BIGINT | (get_byte(_h, 22)::BIGINT << 8)
             | (get_byte(_h, 23)::BIGINT << 16) | (get_byte(_h, 24)::BIGINT << 24);
      _w := (_bits & 16383) + 1;
      _h_px := ((_bits >> 14) & 16383) + 1;
      _alpha := ((_bits >> 28) & 1) = 1;
      _animated := FALSE;
    ELSIF _chunk = 'VP8X' THEN                             -- extended (alpha, animation, metadata)
      _flags := get_byte(_h, 20);
      _animated := (_flags & 2) <> 0;
      _alpha := (_flags & 16) <> 0;
      _w := (get_byte(_h, 24) | (get_byte(_h, 25) << 8) | (get_byte(_h, 26) << 16)) + 1;
      _h_px := (get_byte(_h, 27) | (get_byte(_h, 28) << 8) | (get_byte(_h, 29) << 16)) + 1;
    ELSE
      RETURN NULL;
    END IF;
  ELSE
    RETURN NULL;
  END IF;

  IF _w IS NULL OR _h_px IS NULL OR _w <= 0 OR _h_px <= 0 THEN
    RETURN NULL;
  END IF;

  IF _body_length IS NOT NULL AND _body_length > _prefix_len THEN
    _bytes := ((_body_length - _prefix_len) * 3) / 4;      -- base64 payload size, padding ignored
  END IF;

  RETURN jsonb_strip_nulls(jsonb_build_object(
    'mime', _mime,
    'width', _w,
    'height', _h_px,
    'aspect_ratio', round(_w::NUMERIC / _h_px, 4),
    'animated', _animated,
    'alpha', _alpha,
    'bytes', _bytes
  ));
END
$$;

-- Normalised metadata of a post from its json_metadata text and the start of its body.
CREATE OR REPLACE FUNCTION pixamind.content_metadata(_json TEXT, _body_head TEXT, _body_length INT DEFAULT NULL)
RETURNS JSONB LANGUAGE plpgsql IMMUTABLE AS
$$
DECLARE
  _meta JSONB := pixamind.try_jsonb(_json);
  _app TEXT;
  _format TEXT;
  _tags JSONB;
  _license JSONB;
  _image JSONB;
BEGIN
  IF jsonb_typeof(_meta) IS DISTINCT FROM 'object' THEN
    _meta := '{}'::JSONB;
  END IF;

  _app := CASE WHEN jsonb_typeof(_meta->'app') = 'string' THEN left(_meta->>'app', 64) END;
  _format := CASE WHEN jsonb_typeof(_meta->'format') = 'string' THEN left(_meta->>'format', 32) END;

  IF jsonb_typeof(_meta->'tags') = 'array' THEN
    SELECT jsonb_agg(t ORDER BY ord) INTO _tags
    FROM (
      SELECT left(lower(value #>> '{}'), 64) AS t, ord
      FROM jsonb_array_elements(_meta->'tags') WITH ORDINALITY AS a(value, ord)
      WHERE jsonb_typeof(value) = 'string' AND value #>> '{}' <> ''
      ORDER BY ord
      LIMIT 10
    ) x;
  END IF;

  IF jsonb_typeof(_meta->'license') = 'object' THEN
    _license := jsonb_strip_nulls(jsonb_build_object(
      'type', CASE WHEN jsonb_typeof(_meta->'license'->'type') = 'string' THEN left(_meta->'license'->>'type', 64) END,
      'version', CASE WHEN jsonb_typeof(_meta->'license'->'version') = 'string' THEN left(_meta->'license'->>'version', 16) END,
      'royalty_percentage', CASE WHEN jsonb_typeof(_meta->'license'->'royaltyPercentage') = 'number'
                                 THEN _meta->'license'->'royaltyPercentage' END,
      'is_customized', CASE WHEN jsonb_typeof(_meta->'license'->'isCustomized') = 'boolean'
                            THEN _meta->'license'->'isCustomized' END
    ));
  END IF;

  IF _format = 'image' OR _format IS NULL THEN
    _image := pixamind.image_info(_body_head, _body_length);
  END IF;

  RETURN jsonb_strip_nulls(jsonb_build_object(
    'format', COALESCE(_format, CASE WHEN _image IS NOT NULL THEN 'image' END),
    'app', _app,
    'tags', _tags,
    'description', CASE WHEN jsonb_typeof(_meta->'description') = 'string' THEN left(_meta->>'description', 500) END,
    'nsfw', CASE WHEN jsonb_typeof(_meta->'nsfw') = 'boolean' THEN _meta->'nsfw' END,
    'original_content', CASE WHEN jsonb_typeof(_meta->'oc') = 'boolean' THEN _meta->'oc' END,
    'ai_used', CASE WHEN jsonb_typeof(_meta->'ai') = 'string' THEN NULLIF(btrim(left(_meta->>'ai', 200)), '') END,
    'license', NULLIF(_license, '{}'::JSONB),
    'image', _image
  ));
END
$$;
