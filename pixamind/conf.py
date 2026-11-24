"""Pixamind command-line and environment options.

They are added to the inherited configargparse parser (hive/conf.py), so they
work with `pixamind sync`, `hive sync` and the docker entrypoint alike:

    --pixamind-events / PIXAMIND_EVENTS                         event outbox (default off)
    --pixamind-events-retention-days / PIXAMIND_EVENTS_RETENTION_DAYS (default 30, 0 = forever)
    --pixamind-promotions / PIXAMIND_PROMOTIONS                 promoted posts (default on)
    --pixamind-nfts / PIXAMIND_NFTS                             native NFTs (default on)
    --pixamind-telemetry / PIXAMIND_TELEMETRY                   per-batch telemetry (default on)
    --pixamind-feed-cache / PIXAMIND_FEED_CACHE                 shared feeds cache (default on)
    --pixamind-post-history / PIXAMIND_POST_HISTORY             every version of posts and comments (default on)
"""

from __future__ import annotations

from dataclasses import dataclass

from hive.utils.normalize import strtobool

BLOCKS_PER_DAY = 28_800  # 3-second blocks


def add_arguments(add) -> None:
    """Register the Pixamind options on a configargparse `add` callable."""
    add(
        '--pixamind-events',
        type=strtobool,
        env_var='PIXAMIND_EVENTS',
        default=False,
        help='write the social event outbox (pixamind_api.get_events)',
    )
    add(
        '--pixamind-events-retention-days',
        type=int,
        env_var='PIXAMIND_EVENTS_RETENTION_DAYS',
        default=30,
        help='days of events to keep and to write during a full sync; 0 keeps everything',
    )
    add(
        '--pixamind-promotions',
        type=strtobool,
        env_var='PIXAMIND_PROMOTIONS',
        default=True,
        help='index paid post promotions (transfers to @null) for the promoted feed',
    )
    add(
        '--pixamind-nfts',
        type=strtobool,
        env_var='PIXAMIND_NFTS',
        default=True,
        help='index the native NFTs of the chain (nft_operation) for the pixamind_nft API',
    )
    add(
        '--pixamind-telemetry',
        type=strtobool,
        env_var='PIXAMIND_TELEMETRY',
        default=True,
        help='record per-batch timings for pixamind_api.get_metrics',
    )
    add(
        '--pixamind-feed-cache',
        type=strtobool,
        env_var='PIXAMIND_FEED_CACHE',
        default=True,
        help='compute the observer-less feeds of pixamind.feed_cache_targets once per block (docs/pixamind/FEED-CACHE.md)',
    )
    add(
        '--pixamind-post-history',
        type=strtobool,
        env_var='PIXAMIND_POST_HISTORY',
        default=True,
        help='record every version of posts and comments (docs/pixamind/POST-HISTORY.md)',
    )


@dataclass(frozen=True)
class PixamindSettings:
    events: bool = False
    events_retention_days: int = 30
    promotions: bool = True
    nfts: bool = True
    telemetry: bool = True
    feed_cache: bool = True
    post_history: bool = True

    @property
    def events_keep_blocks(self) -> int:
        """Blocks of events to keep (0 = no pruning)."""
        return max(self.events_retention_days, 0) * BLOCKS_PER_DAY

    @classmethod
    def from_conf(cls, conf) -> PixamindSettings:
        """Read the settings from a hive.conf.Conf (missing options keep their defaults)."""
        args = conf.args() or {}

        def get(name, default):
            value = args.get(name)
            return default if value is None else value

        return cls(
            events=bool(get('pixamind_events', cls.events)),
            events_retention_days=int(get('pixamind_events_retention_days', cls.events_retention_days)),
            promotions=bool(get('pixamind_promotions', cls.promotions)),
            nfts=bool(get('pixamind_nfts', cls.nfts)),
            telemetry=bool(get('pixamind_telemetry', cls.telemetry)),
            feed_cache=bool(get('pixamind_feed_cache', cls.feed_cache)),
            post_history=bool(get('pixamind_post_history', cls.post_history)),
        )
