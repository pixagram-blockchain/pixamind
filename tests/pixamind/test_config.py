"""Configuration: PIXAMIND_* environment with HIVEMIND_* fallback, chain defaults, CLI options."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PROBE = (
    'import json, hive.utils.chain as c; print(json.dumps([c.CHAIN, c.HIVE_SYMBOL, c.HBD_SYMBOL, '
    'c.LEGACY_RSHARES_UPSCALE_BEFORE_BLOCK, c.DEFAULT_COMMUNITY_START_BLOCK, c.CURATION_REWARDS_PERCENT]))'
)


def chain_settings(**env):
    clean = {k: v for k, v in os.environ.items() if not k.startswith(('PIXAMIND_', 'HIVEMIND_'))}
    out = subprocess.run([sys.executable, '-c', PROBE], env={**clean, **env}, capture_output=True, text=True, cwd=REPO)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_pixa_defaults():
    assert chain_settings() == ['pixa', 'PIXA', 'PXS', 0, 1, 4000]


def test_hive_chain_keeps_hivemind_behaviour():
    assert chain_settings(PIXAMIND_CHAIN='hive') == ['hive', 'HIVE', 'HBD', 905693, 37500000, 5000]


def test_legacy_variables_still_work_and_new_ones_win():
    assert chain_settings(HIVEMIND_HIVE_SYMBOL='OLD', HIVEMIND_HBD_SYMBOL='OLDS')[1:3] == ['OLD', 'OLDS']
    assert chain_settings(HIVEMIND_HIVE_SYMBOL='OLD', PIXAMIND_HIVE_SYMBOL='NEW')[1] == 'NEW'
    assert chain_settings(PIXAMIND_HIVE_SYMBOL='  ')[1] == 'PIXA'  # blank means unset


def test_cli_options_and_settings(monkeypatch):
    from hive.conf import Conf
    from pixamind.conf import BLOCKS_PER_DAY, PixamindSettings

    monkeypatch.setattr(
        sys, 'argv', ['pixamind', 'sync', '--database-url', 'postgresql://x@y/z', '--pixamind-events', 'true']
    )
    monkeypatch.setenv('PIXAMIND_EVENTS_RETENTION_DAYS', '7')
    monkeypatch.setenv('PIXAMIND_PROMOTIONS', 'false')
    conf = Conf()
    conf.init_argparse(strict=False)
    settings = PixamindSettings.from_conf(conf)
    assert settings == PixamindSettings(events=True, events_retention_days=7, promotions=False, telemetry=True)
    assert settings.events_keep_blocks == 7 * BLOCKS_PER_DAY
    assert conf.get('community_start_block') == 1


@pytest.mark.parametrize('days, blocks', [(0, 0), (-3, 0), (1, 28800)])
def test_retention_blocks(days, blocks):
    from pixamind.conf import PixamindSettings

    assert PixamindSettings(events_retention_days=days).events_keep_blocks == blocks
