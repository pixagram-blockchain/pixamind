"""Massive sync (Blocks.process_multi_sql) runs the Pixamind phase too, and
indexing a range in one massive batch or block by block gives the same Pixamind
projections: event ids and payloads are a function of the chain alone."""

import harness
import pytest
import scenario
from conftest import Sql
from livesync import Chain, Indexer

PIXAMIND_PROJECTIONS = ['events', 'promotions', 'promoted_posts']


def _sync(dsn: str, massive: bool) -> list:
    idx = Indexer(dsn)
    try:
        scenario.load(Chain(idx.db))
        if massive:
            idx.process_massive(1, 6)
        else:
            for block in range(1, 7):
                idx.process_live(block)
    finally:
        idx.close()
    sql = Sql(dsn)
    try:
        return sql.all(
            'SELECT projection, rows, fingerprint FROM pixamind.projection_fingerprints(%s)', PIXAMIND_PROJECTIONS
        )
    finally:
        sql.close()


def test_massive_batch_runs_pixamind_phase(pg_server):
    dsn = harness.build_database()
    _sync(dsn, massive=True)
    sql = Sql(dsn)
    try:
        batch = sql.all("SELECT mode, first_block, last_block, ops, phases FROM pixamind.sync_batches")
        assert len(batch) == 1
        mode, first, last, ops, phases = batch[0]
        assert (mode, first, last) == ('massive', 1, 6) and ops > 0
        assert {'load', 'posts', 'votes', 'notify'} <= set(phases)
        assert sql.one('SELECT count(*) FROM pixamind.events') == 20  # same stream as test_live_sync.test_event_stream
        assert sql.one('SELECT count(*) FROM pixamind.projection_failures') == 0
    finally:
        sql.close()


@pytest.mark.parametrize('second_db', ['pixamind_test_live'])
def test_massive_and_live_agree(pg_server, second_db):
    massive = _sync(harness.build_database(), massive=True)
    live = _sync(harness.build_database(second_db), massive=False)
    assert massive == live
