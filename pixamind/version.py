"""Pixamind version information.

The version is computed from git by versioningit at build time (see
pyproject.toml) and shared with the inherited ``hive`` package.
"""

from typing import Final

from hive.version import GIT_DATE, GIT_REVISION, VERSION

# Upstream Hivemind release this code base was last merged with.
HIVEMIND_BASE: Final[str] = '1.28.8'

__all__ = ['GIT_DATE', 'GIT_REVISION', 'HIVEMIND_BASE', 'VERSION']
