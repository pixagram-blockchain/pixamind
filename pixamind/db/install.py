"""Installer for the ``pixamind`` database schema.

Two kinds of SQL live in pixamind/sql:

* ``migrations/NNNN_name.sql`` — tables and indexes. Each runs once, inside a
  transaction, and is recorded in ``pixamind.schema_migrations`` with a checksum.
  Never edit an applied migration: add a new one.
* ``runtime/*.sql`` — functions and API methods. Reinstalled (CREATE OR REPLACE)
  on every indexer start, in file-name order, like hivemind's runtime code.

Both go through hivemind's SQL loader, so ``{{HBD_SYMBOL}}``-style templates
work the same way. Installation is fail-safe: if anything goes wrong it is
logged and Pixamind features stay off for this run, but the inherited social
indexer always starts.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = 'pixamind'
SQL_DIR = Path(__file__).resolve().parent.parent / 'sql'
MIGRATIONS_DIR = SQL_DIR / 'migrations'
RUNTIME_DIR = SQL_DIR / 'runtime'


def migration_files() -> list[tuple[int, Path]]:
    """(version, path) of every migration, in order."""
    found = []
    for path in sorted(MIGRATIONS_DIR.glob('[0-9][0-9][0-9][0-9]_*.sql')):
        found.append((int(path.name[:4]), path))
    versions = [v for v, _ in found]
    assert len(versions) == len(set(versions)), f'duplicate migration versions in {MIGRATIONS_DIR}'
    return found


def runtime_files() -> list[Path]:
    return sorted(RUNTIME_DIR.glob('*.sql'))


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def schema_exists(db) -> bool:
    return bool(db.query_one(f"SELECT 1 FROM pg_namespace WHERE nspname = '{SCHEMA}'"))


def ensure_schema(db, owner: str) -> bool:
    """Create the schema when missing. Needs CREATE on the database (the setup
    step runs as haf_admin and has it); the sync role usually does not, which is
    fine once the schema exists. Returns whether the schema exists afterwards."""
    if schema_exists(db):
        return True
    try:
        db.query_no_return(f'CREATE SCHEMA IF NOT EXISTS {SCHEMA} AUTHORIZATION {owner}')
        log.info("Created schema '%s'", SCHEMA)
        return True
    except Exception as ex:  # noqa: BLE001 - reported, never fatal
        log.warning(
            "Could not create schema '%s' (%s). Run the setup step once with the HAF admin "
            "role (docker entrypoint `setup`, or `pixamind build_schema --database-url=<admin url>`).",
            SCHEMA,
            ex,
        )
        return False


def _execute(db, path: Path) -> None:
    from hive.db.schema import execute_sql_script

    execute_sql_script(db.query_no_return, path)


def apply_migrations(db) -> list[int]:
    """Apply pending migrations; return the versions applied now."""
    db.query_no_return(
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.schema_migrations (
                version INT PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )"""
    )
    applied = {row[0]: row[1] for row in db.query_all(f'SELECT version, checksum FROM {SCHEMA}.schema_migrations')}
    done = []
    for version, path in migration_files():
        digest = checksum(path)
        if version in applied:
            if applied[version] != digest:
                log.warning(
                    'Pixamind migration %s changed after it was applied; edits to applied migrations are ignored',
                    path.name,
                )
            continue
        log.info('Applying Pixamind migration %s', path.name)
        db.query_no_return('START TRANSACTION')
        try:
            _execute(db, path)
            db.query_no_return(
                f'INSERT INTO {SCHEMA}.schema_migrations (version, name, checksum) VALUES (:version, :name, :checksum)',
                version=version,
                name=path.name,
                checksum=digest,
            )
            db.query_no_return('COMMIT')
        except Exception:
            db.query_no_return('ROLLBACK')
            raise
        done.append(version)
    return done


def install_runtime(db) -> None:
    for path in runtime_files():
        _execute(db, path)


def grant_read_access(db, role: str = 'hivemind_user') -> None:
    if not db.query_one(f"SELECT 1 FROM pg_roles WHERE rolname = '{role}'"):
        return
    db.query_no_return(f'GRANT USAGE ON SCHEMA {SCHEMA} TO {role}')
    db.query_no_return(f'GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO {role}')
    db.query_no_return(f'ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} GRANT SELECT ON TABLES TO {role}')


def write_build_settings(db) -> None:
    from hive.utils.chain import CHAIN
    from pixamind.version import GIT_REVISION, HIVEMIND_BASE, VERSION

    for name, value in (
        ('version', VERSION),
        ('hivemind_base', HIVEMIND_BASE),
        ('git_revision', GIT_REVISION),
        ('chain', CHAIN),
    ):
        db.query_no_return('SELECT pixamind.set_setting(:name, :value)', name=name, value=str(value))


def install(db, owner: str = 'hivemind') -> bool:
    """Install or upgrade the Pixamind schema. Never raises; returns success."""
    try:
        if not ensure_schema(db, owner):
            return False
        applied = apply_migrations(db)
        install_runtime(db)
        grant_read_access(db)
        write_build_settings(db)
        log.info('Pixamind schema ready (migrations applied now: %s)', applied or 'none')
        return True
    except Exception:
        log.exception('Pixamind schema installation failed; Pixamind features are off for this run')
        try:
            if db.is_trx_active():
                db.query_no_return('ROLLBACK')
        except Exception:  # noqa: BLE001
            pass
        return False
