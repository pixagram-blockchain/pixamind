"""The Pixamind installer and the fail-safe guarantees of the sync hooks."""

import re

import scenario

from pixamind.db import install


def test_migrations_recorded_once(sql, dsn):
    rows = sql.all('SELECT version, name, checksum FROM pixamind.schema_migrations ORDER BY version')
    assert [r[0] for r in rows] == [v for v, _ in install.migration_files()]
    assert all(r[2] == install.checksum(path) for r, (_, path) in zip(rows, install.migration_files(), strict=True))

    # a second install (every indexer start) applies nothing and keeps the data
    from hive.db.adapter import Db

    sql.run("SELECT pixamind.set_setting('probe', 'kept')")
    db = Db(dsn, 'pixamind_test_reinstall')
    try:
        assert install.install(db) is True
        assert install.apply_migrations(db) == []
    finally:
        db.close()
    assert sql.one("SELECT pixamind.get_setting('probe')") == 'kept'
    assert sql.one("SELECT pixamind.get_setting('chain')") == 'pixa'


def test_install_never_raises(sql, dsn):
    """Without the right to create the schema, install() reports and returns False."""
    from hive.db.adapter import Db

    sql.run('DROP SCHEMA pixamind CASCADE')
    db = Db(dsn, 'pixamind_test_noschema')  # the app role may not CREATE schemas here
    try:
        assert install.install(db) is False
    finally:
        db.close()


def test_failing_projection_is_recorded_not_fatal(indexer, chain, sql):
    scenario.load(chain)
    sql.run('DROP FUNCTION pixamind.process_promotions(INT, INT)')
    for block in range(1, 5):
        indexer.process_live(block)
    # hivemind's own work committed for every block...
    assert sql.one("SELECT last_completed_block_num FROM hivemind_app.hive_state") == 4
    assert sql.one("SELECT count(*) FROM hivemind_app.hive_posts WHERE id > 0") == 3
    # ...the event outbox too; the broken step is on record, once per block
    assert sql.one('SELECT count(*) FROM pixamind.events') > 0
    failures = sql.all('SELECT projection, first_block, last_block FROM pixamind.projection_failures ORDER BY id')
    assert failures == [('promotions', b, b) for b in range(1, 5)]
    metrics = sql.result('pixamind_api.get_metrics')
    assert metrics['failures']['open'] == 4


def test_broken_hook_cannot_abort_a_live_block(indexer, chain, sql):
    scenario.load(chain)
    sql.run('DROP FUNCTION pixamind.after_batch(TEXT, INT, INT, REAL, JSONB, JSONB)')
    for block in range(1, 4):
        indexer.process_live(block)  # the hook fails inside the block's transaction
    assert sql.one("SELECT last_completed_block_num FROM hivemind_app.hive_state") == 3
    assert sql.one("SELECT count(*) FROM hivemind_app.hive_votes") == 1


def test_sql_has_no_bind_parameter_lookalikes():
    """hive.db.adapter turns `:name` into a bind parameter in every statement it
    runs, including SQL scripts; a stray `:word` (e.g. `[[:space:]]`) breaks them."""
    pattern = re.compile(r'(?<![:\w]):([a-zA-Z_]\w*)')
    offenders = []
    for path in install.runtime_files() + [p for _, p in install.migration_files()]:
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line.split('--')[0]):
                offenders.append(f'{path.name}:{number}: {line.strip()}')
    assert not offenders, offenders


def test_migration_files_are_numbered():
    versions = [v for v, _ in install.migration_files()]
    assert versions == list(range(1, len(versions) + 1))


def test_broken_hook_cannot_fail_a_massive_batch(indexer, chain, sql):
    scenario.load(chain)
    sql.run('DROP FUNCTION pixamind.after_batch(TEXT, INT, INT, REAL, JSONB, JSONB)')
    indexer.process_massive(1, 6)  # the Pixamind phase fails, the batch does not
    assert sql.one('SELECT count(*) FROM hivemind_app.hive_posts WHERE id > 0') == 3
    assert sql.one('SELECT count(*) FROM hivemind_app.follows') == 3
