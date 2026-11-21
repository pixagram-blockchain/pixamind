"""Prometheus metrics of the Pixamind indexer.

Metrics register in prometheus_client's default registry, which the HTTP
server started by ``--prometheus-port`` (hive/utils/stats.py) already serves,
so enabling that option is all it takes to scrape them. Without it they cost
next to nothing.

    pixamind_build_info{version, hivemind_base, chain}
    pixamind_indexer_last_block                      last block committed by the indexer
    pixamind_indexer_blocks_total{mode}              blocks processed (mode: massive | live)
    pixamind_indexer_batches_total{mode}
    pixamind_indexer_batch_seconds{mode}             histogram of batch / block processing time
    pixamind_indexer_phase_seconds_total{mode,phase} time per processing phase
    pixamind_block_to_commit_seconds                 live: block timestamp -> indexed (histogram)
    pixamind_events_written_total                    event outbox rows written
    pixamind_promotions_indexed_total                promotion payments indexed
    pixamind_projection_failures_total               Pixamind projection steps that failed
    pixamind_feed_cache_refresh_seconds              shared feeds cache: refresh after a live block (histogram)
    pixamind_feed_cache_rewritten_total              shared feeds cache: results that changed
    pixamind_feed_cache_failures_total               shared feeds cache: targets not computed, failed refreshes
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

try:
    from prometheus_client import Counter, Gauge, Histogram, Info

    _AVAILABLE = True
except ImportError:  # pragma: no cover - prometheus_client is a hard dependency
    _AVAILABLE = False

_BATCH_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)
_LATENCY_BUCKETS = (0.5, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 60, 120, 300)

if _AVAILABLE:
    BUILD_INFO = Info('pixamind_build', 'Pixamind build and chain')
    LAST_BLOCK = Gauge('pixamind_indexer_last_block', 'Last block committed by the indexer')
    BLOCKS = Counter('pixamind_indexer_blocks', 'Blocks processed', ['mode'])
    BATCHES = Counter('pixamind_indexer_batches', 'Batches processed', ['mode'])
    BATCH_SECONDS = Histogram(
        'pixamind_indexer_batch_seconds', 'Batch processing time', ['mode'], buckets=_BATCH_BUCKETS
    )
    PHASE_SECONDS = Counter('pixamind_indexer_phase_seconds', 'Time spent per processing phase', ['mode', 'phase'])
    BLOCK_TO_COMMIT = Histogram(
        'pixamind_block_to_commit_seconds', 'Live sync: block timestamp to indexed', buckets=_LATENCY_BUCKETS
    )
    EVENTS = Counter('pixamind_events_written', 'Event outbox rows written')
    PROMOTIONS = Counter('pixamind_promotions_indexed', 'Promotion payments indexed')
    FAILURES = Counter('pixamind_projection_failures', 'Pixamind projection steps that failed')
    FEED_CACHE_SECONDS = Histogram(
        'pixamind_feed_cache_refresh_seconds', 'Shared feeds cache: refresh after a live block', buckets=_BATCH_BUCKETS
    )
    FEED_CACHE_REWRITTEN = Counter('pixamind_feed_cache_rewritten', 'Shared feeds cache: results that changed')
    FEED_CACHE_FAILURES = Counter(
        'pixamind_feed_cache_failures', 'Shared feeds cache: targets that could not be computed, and failed refreshes'
    )


def set_build_info(version: str, hivemind_base: str, chain: str) -> None:
    if _AVAILABLE:
        BUILD_INFO.info({'version': str(version), 'hivemind_base': hivemind_base, 'chain': chain})


def observe_batch(mode: str, first_block: int, last_block: int, seconds: float, phases: dict, summary: dict) -> None:
    """Record one processed batch (massive) or block (live). Never raises."""
    if not _AVAILABLE:
        return
    try:
        LAST_BLOCK.set(last_block)
        BLOCKS.labels(mode).inc(max(last_block - first_block + 1, 0))
        BATCHES.labels(mode).inc()
        BATCH_SECONDS.labels(mode).observe(max(seconds, 0.0))
        for phase, value in (phases or {}).items():
            if isinstance(value, (int, float)) and value >= 0:
                PHASE_SECONDS.labels(mode, phase).inc(value)
        summary = summary or {}
        if summary.get('latency_seconds') is not None:
            BLOCK_TO_COMMIT.observe(max(float(summary['latency_seconds']), 0.0))
        EVENTS.inc(max(int(summary.get('events', 0) or 0), 0))
        PROMOTIONS.inc(max(int(summary.get('promotions', 0) or 0), 0))
        FAILURES.inc(max(int(summary.get('failures', 0) or 0), 0))
    except Exception:  # noqa: BLE001 - metrics must never break indexing
        log.debug('Could not record Pixamind metrics', exc_info=True)


def count_failure() -> None:
    if _AVAILABLE:
        FAILURES.inc()


def observe_feed_cache(seconds: float, summary: dict | None) -> None:
    """Record one refresh of the shared feeds cache (summary None: the refresh failed). Never raises."""
    if not _AVAILABLE:
        return
    try:
        if summary is None:
            FEED_CACHE_FAILURES.inc()
            return
        if summary.get('skipped'):  # catching up: nothing was computed
            return
        FEED_CACHE_SECONDS.observe(max(seconds, 0.0))
        FEED_CACHE_REWRITTEN.inc(max(int(summary.get('changed', 0) or 0), 0))
        FEED_CACHE_FAILURES.inc(max(int(summary.get('failed', 0) or 0), 0))
    except Exception:  # noqa: BLE001 - metrics must never break indexing
        log.debug('Could not record feed cache metrics', exc_info=True)
