"""Install the real hivemind + Pixamind SQL on a plain PostgreSQL server.

HAF and reputation_tracker are replaced by the small stand-ins in
``tests/pixamind/sql/``; everything else — table definitions, the runtime SQL
that ``hive build_schema`` installs, the Pixamind schema — is the production
code, installed through the same Python entry points the indexer uses.

The server is taken from ``PIXAMIND_TEST_ADMIN_DSN`` (a superuser DSN, default
``postgresql://postgres@/postgres``). Tests that need it are skipped when no
server is reachable.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg2
import psycopg2.extensions

HERE = Path(__file__).resolve().parent
SQL_DIR = HERE / 'sql'
TEST_DB = os.environ.get('PIXAMIND_TEST_DB', 'pixamind_test')
ADMIN_DSN = os.environ.get('PIXAMIND_TEST_ADMIN_DSN', 'postgresql://postgres@/postgres')


def _connect(dsn: str, **kwargs):
    conn = psycopg2.connect(dsn, **kwargs)
    conn.autocommit = True
    return conn


def server_available() -> bool:
    try:
        _connect(ADMIN_DSN, connect_timeout=3).close()
        return True
    except psycopg2.Error:
        return False


APP_ROLE_PASSWORD = 'pixamind-test'


def test_dsn(name: str = TEST_DB) -> str:
    """Superuser DSN of a test database (fixtures, stub HAF edits)."""
    return psycopg2.extensions.make_dsn(ADMIN_DSN, dbname=name)


def app_dsn(name: str = TEST_DB) -> str:
    """DSN of a test database as the `hivemind` role, which is what the indexer
    and its installer log in as in production."""
    return psycopg2.extensions.make_dsn(ADMIN_DSN, dbname=name, user='hivemind', password=APP_ROLE_PASSWORD)


def _execute_file(conn, path: Path) -> None:
    with conn.cursor() as cur:
        cur.execute(path.read_text())


def build_database(name: str = TEST_DB) -> str:
    """(Re)create a test database and install everything. Returns the app-role DSN."""
    admin = _connect(ADMIN_DSN)
    with admin.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        cur.execute(f'CREATE DATABASE "{name}"')
        for role in ('hivemind', 'hivemind_user'):
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE ROLE {role} LOGIN')
        cur.execute(f"ALTER ROLE hivemind PASSWORD '{APP_ROLE_PASSWORD}'")
    admin.close()

    conn = _connect(test_dsn(name))
    _execute_file(conn, SQL_DIR / 'haf_stub.sql')
    with conn.cursor() as cur:
        # hive.db.schema.setup() creates these with the admin connection.
        for schema in ('hivemind_app', 'hivemind_endpoints', 'hivemind_postgrest_utilities', 'pixamind'):
            cur.execute(f'CREATE SCHEMA {schema} AUTHORIZATION hivemind')
        cur.execute(
            'GRANT USAGE ON SCHEMA hafd, hive, reptracker_app, reptracker_endpoints, pixamind_test TO hivemind, hivemind_user'
        )
        cur.execute('GRANT SELECT ON ALL TABLES IN SCHEMA hafd, hive, reptracker_app TO hivemind, hivemind_user')
        # the stub HAF stands in for hived: the tests drive it through the app role
        cur.execute('GRANT ALL ON ALL TABLES IN SCHEMA hafd TO hivemind')
        cur.execute('GRANT ALL ON ALL SEQUENCES IN SCHEMA hafd TO hivemind')
    _execute_file(conn, SQL_DIR / 'haf_context_views.sql')
    with conn.cursor() as cur:
        cur.execute('ALTER VIEW hivemind_app.blocks_view OWNER TO hivemind')
        cur.execute('ALTER VIEW hivemind_app.operations_view OWNER TO hivemind')
        cur.execute('ALTER VIEW hivemind_app.accounts_view OWNER TO hivemind')
        cur.execute('ALTER VIEW hivemind_app.context_data_view OWNER TO hivemind')
    conn.close()

    # The production installer, minus the HAF-only steps (context creation,
    # index registration, plpython3u extension).
    from hive.db import schema
    from hive.db.adapter import Db

    dsn = app_dsn(name)
    db = Db(dsn, 'pixamind_test_setup')  # like DbState.initialize(): logged in as the app role
    try:
        schema.execute_sql_script(
            db.query_no_return, schema.Path(schema.__file__).parent / 'sql_scripts' / 'schema' / 'create_tables.sql'
        )
        schema.insert_seed_data(db)
        db.query_no_return(
            """CREATE TABLE IF NOT EXISTS hivemind_app.hive_db_patch_level
               (level SERIAL NOT NULL PRIMARY KEY, patch_date timestamp without time zone NOT NULL, patched_to_revision TEXT)"""
        )
        db.query_no_return('SET SEARCH_PATH TO reptracker_app')
        db.query_no_return("SET custom.swagger_url = '{hivemind-host}'")
        schema.setup_runtime_code(db)
    finally:
        db.close()
    return dsn
