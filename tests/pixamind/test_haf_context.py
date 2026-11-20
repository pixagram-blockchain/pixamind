"""Pixamind indexes irreversible blocks only: its HAF context is non-forking.

Pixagram's earlier build ran a forking context with every hivemind table registered
with HAF's fork manager (docs/pixamind/ARCHITECTURE.md §3.2). These tests keep that
decision from coming back unnoticed, e.g. through a merge from the old fork.
"""

from __future__ import annotations

import inspect
import logging

from hive.db import schema
from hive.indexer.hive_db import haf_functions
from pixamind.conf import PixamindSettings
from pixamind.indexer.hooks import PixamindHooks


class RecordingDb:
    """Stands in for hive.db.adapter.Db; records the SQL prepare_app_context sends."""

    def __init__(self, context_exists: bool):
        self.context_exists = context_exists
        self.statements: list[str] = []

    def query_one(self, sql: str, **_):
        self.statements.append(sql)
        if 'app_context_exists' in sql:
            return self.context_exists
        return False  # app_is_forking

    def query_no_return(self, sql: str, **_) -> None:
        self.statements.append(sql)


def test_a_new_context_is_created_non_forking():
    db = RecordingDb(context_exists=False)
    haf_functions.prepare_app_context(db)
    created = [s for s in db.statements if 'hive.app_create_context' in s]
    assert len(created) == 1
    assert '_is_forking => FALSE' in created[0]


def test_an_existing_context_is_switched_to_non_forking():
    db = RecordingDb(context_exists=True)
    haf_functions.prepare_app_context(db)
    assert any('hive.app_context_set_non_forking' in s for s in db.statements)
    assert not any('app_context_set_forking' in s or 'app_create_context' in s for s in db.statements)


def test_setup_does_not_register_tables_with_the_fork_manager():
    # Registration is for forking contexts, and registered tables keep massive sync
    # from dropping indexes and foreign keys (hive/db/db_state.py).
    assert 'app_register_table' not in inspect.getsource(schema)


def test_sync_start_flags_a_context_that_is_still_forking(indexer, sql, caplog):
    with caplog.at_level(logging.WARNING, logger='pixamind.indexer.hooks'):
        PixamindHooks.configure(None, indexer.db, PixamindSettings())
    assert 'still forking' not in caplog.text

    sql.run("UPDATE hafd.contexts SET is_forking = TRUE WHERE name = 'hivemind_app'")  # an old database
    with caplog.at_level(logging.WARNING, logger='pixamind.indexer.hooks'):
        PixamindHooks.configure(None, indexer.db, PixamindSettings())
    assert 'still forking' in caplog.text
    assert PixamindHooks.enabled  # informational: the hooks still run


def test_pixamind_does_not_block_the_unlogged_massive_sync(indexer, sql):
    # Without registration, an initial massive sync turns hivemind's large tables
    # UNLOGGED and back (DbState.ensure_indexes_are_disabled/enabled). PostgreSQL
    # refuses that while a logged table references them, so nothing in the pixamind
    # schema may hold a foreign key to hivemind_app.
    persistence = "SELECT relpersistence FROM pg_class WHERE oid = 'hivemind_app.hive_posts'::regclass"
    schema.set_logged_table_attribute(indexer.db, False)
    assert sql.one(persistence) == 'u'
    schema.set_logged_table_attribute(indexer.db, True)
    assert sql.one(persistence) == 'p'
