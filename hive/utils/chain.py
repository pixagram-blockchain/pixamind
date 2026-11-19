"""Chain-specific configuration: asset symbols and chain-history corrections.

Pixamind is built for the Pixa chain, a Hive fork with its own genesis. A few
constants inherited from Hivemind only make sense on a chain that carries the
2016 Steem history (Hive mainnet, which the upstream API test suite uses), so
they are selected by ``PIXAMIND_CHAIN``:

    PIXAMIND_CHAIN=pixa  (default)  PIXA / PXS symbols, no Steem-history corrections
    PIXAMIND_CHAIN=hive             HIVE / HBD symbols, Hivemind's historical behaviour

Every value can still be overridden individually through the environment.
"""

from __future__ import annotations

import os
from typing import Final


def _env(names: tuple[str, ...], default: str) -> str:
    """Return the first non-empty environment variable among `names`, else `default`.

    Pixamind reads its own PIXAMIND_* names first and falls back to the legacy
    HIVEMIND_* names, so existing deployments keep working unchanged.
    """
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


CHAIN: Final[str] = _env(("PIXAMIND_CHAIN",), "pixa").lower()
_STEEM_HISTORY: Final[bool] = CHAIN in ("hive", "steem")

# Canonical asset symbols for the chain (liquid token, stable token).
HIVE_SYMBOL: Final[str] = _env(("PIXAMIND_HIVE_SYMBOL", "HIVEMIND_HIVE_SYMBOL"), "HIVE" if _STEEM_HISTORY else "PIXA")
HBD_SYMBOL: Final[str] = _env(("PIXAMIND_HBD_SYMBOL", "HIVEMIND_HBD_SYMBOL"), "HBD" if _STEEM_HISTORY else "PXS")
VESTS_SYMBOL: Final[str] = "VESTS"

# Steem's HF1 (block 905,693, April 2016) rescaled rshares by 10^6; Hivemind
# multiplies the rshares of votes cast before it. On a chain with its own genesis
# those block numbers are the chain's first month, so the correction must be off
# there (0 disables it), or early votes are stored a million times too large.
LEGACY_RSHARES_UPSCALE_BEFORE_BLOCK: Final[int] = int(
    _env(("PIXAMIND_LEGACY_RSHARES_UPSCALE_BEFORE_BLOCK",), "905693" if _STEEM_HISTORY else "0")
)

# First block where communities are processed (Hive enabled them at 37.5M).
DEFAULT_COMMUNITY_START_BLOCK: Final[int] = int(
    _env(("PIXAMIND_COMMUNITY_START_BLOCK",), "37500000" if _STEEM_HISTORY else "1")
)

# Curators' share of post rewards in basis points (pending-reward endpoints):
# 40% on Pixa, 50% on Hive since HF21.
CURATION_REWARDS_PERCENT: Final[int] = int(
    _env(("PIXAMIND_CURATION_REWARDS_PERCENT",), "5000" if _STEEM_HISTORY else "4000")
)


LEGACY_SYMBOL_ALIASES: Final[dict[str, str]] = {
    HIVE_SYMBOL: HIVE_SYMBOL,
    HBD_SYMBOL: HBD_SYMBOL,
    VESTS_SYMBOL: VESTS_SYMBOL,
    "HIVE": HIVE_SYMBOL,
    "HBD": HBD_SYMBOL,
    "STEEM": HIVE_SYMBOL,
    "SBD": HBD_SYMBOL,
}


def canonical_symbol(symbol: str) -> str:
    """Return canonical symbol for a possibly legacy symbol."""
    return LEGACY_SYMBOL_ALIASES.get(symbol, symbol)


NAI_MAP: Final[dict[str, str]] = {
    "@@000000013": HBD_SYMBOL,
    "@@000000021": HIVE_SYMBOL,
    "@@000000037": VESTS_SYMBOL,
}

NAI_PRECISION: Final[dict[str, int]] = {
    "@@000000013": 3,
    "@@000000021": 3,
    "@@000000037": 6,
}

UNIT_NAI: Final[dict[str, str]] = {
    HBD_SYMBOL: "@@000000013",
    HIVE_SYMBOL: "@@000000021",
    VESTS_SYMBOL: "@@000000037",
}


def sql_template_vars() -> dict[str, str]:
    """Variables for SQL template substitution ({{NAME}} in the SQL scripts)."""
    return {
        "HIVE_SYMBOL": HIVE_SYMBOL,
        "HBD_SYMBOL": HBD_SYMBOL,
        "VESTS_SYMBOL": VESTS_SYMBOL,
        "LEGACY_RSHARES_UPSCALE_BEFORE_BLOCK": str(LEGACY_RSHARES_UPSCALE_BEFORE_BLOCK),
        "CURATION_REWARDS_PERCENT": str(CURATION_REWARDS_PERCENT),
    }
