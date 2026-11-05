"""Robot naming (iron rule 4, AGENTS.md): the name comes from config.

DEFAULT_ROBOT_NAME is the single source of the factory default. Every
runtime string must be built from an injected ``robot_name`` parameter
(defaulting to this constant) -- the literal must never appear anywhere
else in production code (guarded by tests/test_guards.py GUARD-05).
"""

from __future__ import annotations

DEFAULT_ROBOT_NAME = "小饼"
