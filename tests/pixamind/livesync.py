"""Drive the real hivemind block processor over synthetic blocks.

`Chain` appends operations to the stub HAF tables; `process_live` and
`process_massive` then run the production code paths —
`Blocks.process_live_block_sql` and `Blocks.process_multi_sql` — including the
Pixamind hooks, exactly as the sync loop would after hive.app_next_iteration
returned that block range.
"""

from __future__ import annotations

import json

from hive.db.adapter import Db
from hive.indexer.accounts import Accounts
from hive.indexer.blocks import Blocks
from hive.indexer.community import Community
from hive.indexer.db_adapter_holder import DbAdapterHolder, DbLiveContextHolder
from pixamind.conf import PixamindSettings
from pixamind.indexer.hooks import PixamindHooks


class Chain:
    """Builds blocks of operations in the stub HAF database."""

    def __init__(self, db: Db):
        self.db = db

    def add(self, block: int, *ops: tuple[int, dict]) -> list[int]:
        ids = []
        for op_type, body in ops:
            ids.append(
                self.db.query_one(
                    'SELECT pixamind_test.add_op(:block, CAST(:op_type AS SMALLINT), CAST(:body AS JSONB))',
                    block=block,
                    op_type=op_type,
                    body=json.dumps(body),
                )
            )
        if not ops:
            self.db.query_no_return('SELECT pixamind_test.add_block(:block)', block=block)
        return ids

    def add_account(self, block: int, name: str) -> None:
        """Accounts exist in HAF too (accounts_view feeds hive_accounts.haf_id)."""
        self.db.query_no_return(
            'INSERT INTO hafd.accounts (name, block_num) VALUES (:name, :block) ON CONFLICT DO NOTHING',
            name=name,
            block=block,
        )


class Indexer:
    """The production block processor, pointed at the test database."""

    def __init__(self, dsn: str, settings: PixamindSettings | None = None, community_start_block: int = 1):
        self.db = Db(dsn, 'pixamind_test_sync')
        Db.set_shared_instance(self.db)
        Community.start_block = community_start_block
        Blocks._last_safe_cashout_block = 0
        Blocks._notification_min_block = None
        Accounts._ids = {}
        Accounts.load_ids()
        PixamindHooks.configure(
            None, self.db, settings or PixamindSettings(events=True, promotions=True, telemetry=True)
        )
        assert PixamindHooks.enabled
        self._live = None

    def _set_context(self, current: int, irreversible: int | None = None, stage: str = 'live') -> None:
        self.db.query_no_return(
            'SELECT pixamind_test.set_context(:cur, :irr, :stage)',
            cur=current,
            irr=current if irreversible is None else irreversible,
            stage=stage,
        )

    def _use_live(self, live: bool) -> None:
        if self._live is live:
            return
        if self._live is not None:
            Blocks.close_own_db_access()
        DbLiveContextHolder.set_live_context(live)
        Blocks.setup_own_db_access(shared_db_adapter=self.db)
        self._live = live

    def process_live(self, block: int, irreversible: int | None = None) -> None:
        self._use_live(True)
        self._set_context(block, irreversible)
        self.db.query_no_return('START TRANSACTION')
        Blocks.process_live_block_sql(block, block)

    def process_massive(self, first: int, last: int) -> None:
        from hive.db.db_state import DbState

        DbState._db = self.db
        self._use_live(False)
        self._set_context(last, last, 'MASSIVE_WITH_INDEXES')
        Blocks.process_multi_sql(first, last)

    def close(self) -> None:
        if self._live is not None:
            Blocks.close_own_db_access()
            self._live = None
        DbAdapterHolder.close_common_blocks_in_background_processing_db()
        self.db.close()
