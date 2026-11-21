-- Pixamind runtime: no JIT compilation for API calls.
--
-- Everything PostgREST or pixamind-api can call lives in hivemind_endpoints, and
-- each of those functions gets `SET jit = off`. On hivemind's big feed queries
-- LLVM compilation takes tens to hundreds of milliseconds, often longer than the
-- query, and the result is never reused. scripts/install_app.sql turns JIT off
-- for the whole database as well, but a database-level setting is lost when a
-- dump is restored into a new database; a function's own setting is not.
-- hivemind's runtime code recreates these functions on every start, so this runs
-- on every start too (home() already carries the setting in home.sql).
DO $$
DECLARE
  _fn REGPROCEDURE;
BEGIN
  FOR _fn IN
    SELECT p.oid::REGPROCEDURE
      FROM pg_proc p
     WHERE p.pronamespace = 'hivemind_endpoints'::REGNAMESPACE
       AND p.prokind = 'f'
       AND NOT EXISTS (SELECT 1 FROM unnest(p.proconfig) AS cfg WHERE lower(cfg) LIKE 'jit=%')
  LOOP
    BEGIN
      EXECUTE format('ALTER FUNCTION %s SET jit = off', _fn);
    EXCEPTION WHEN insufficient_privilege THEN
      RAISE WARNING 'pixamind: could not turn JIT off for % (owned by another role)', _fn;
    END;
  END LOOP;
END
$$;

-- What get_info reports: JIT inside API calls, and the database-level default
-- (NULL when the database has no jit setting of its own).
CREATE OR REPLACE FUNCTION pixamind.jit_state()
RETURNS JSONB LANGUAGE sql STABLE AS
$$
  SELECT jsonb_build_object(
    'api', current_setting('jit'),
    'database', (SELECT split_part(cfg, '=', 2)
                   FROM pg_db_role_setting s, unnest(s.setconfig) AS cfg
                  WHERE s.setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database())
                    AND s.setrole = 0 AND lower(cfg) LIKE 'jit=%'
                  LIMIT 1))
$$;
