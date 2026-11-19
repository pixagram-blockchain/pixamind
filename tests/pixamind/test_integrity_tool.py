"""`pixamind integrity` against real databases built by the harness."""

import harness
import scenario
from livesync import Chain, Indexer

from pixamind.tools import integrity


def _build(name: str) -> str:
    dsn = harness.build_database(name)
    idx = Indexer(dsn)
    try:
        scenario.load(Chain(idx.db))
        for block in range(1, 7):
            idx.process_live(block)
    finally:
        idx.close()
    return harness.test_dsn(name)


def test_identical_histories_agree_and_a_difference_is_located(pg_server, capsys):
    left = _build('pixamind_test')
    right = _build('pixamind_test_right')

    assert integrity.main(['--database-url', left, '--compare-url', right]) == 0
    assert capsys.readouterr().out.strip().endswith('EQUAL')

    conn = integrity.connect(right)
    integrity.query(
        conn,
        "DELETE FROM hivemind_app.follows WHERE follower = (SELECT id FROM hivemind_app.hive_accounts WHERE name = 'carol')",
    )
    assert integrity.main(['--database-url', left, '--compare-url', right, '--projections', 'follows,votes']) == 1
    out = capsys.readouterr().out
    assert 'follows' in out and 'DIFFERENT' in out
    assert 'only left  carol|alice|3' in out
    assert 'votes                         1          1  ok' in out


def test_single_database_and_original_hivemind(pg_server, capsys):
    dsn = _build('pixamind_test')
    assert integrity.main(['--database-url', dsn, '--projections', 'events']) == 0
    assert 'events' in capsys.readouterr().out

    # an original Hivemind database has no pixamind schema at all
    conn = integrity.connect(dsn)
    integrity.query(conn, 'DROP SCHEMA pixamind CASCADE')
    assert integrity.main(['--database-url', dsn]) == 2
    capsys.readouterr()
    assert integrity.main(['--database-url', dsn, '--install-functions', '--json']) == 0
    out = capsys.readouterr().out
    assert '"posts"' in out and '"events"' not in out
