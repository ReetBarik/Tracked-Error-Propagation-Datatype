"""Interop-shim kit: generate and cache LLM-written shims that make a target
library callable with Tracked<T>.

- ``ruleset.txt`` — the classification ruleset (Rules 1-9 + C1-C7). C1-C7
  follow from the Tracked API surface itself and hold for ANY target library;
  the text is a usable checklist for a human integrator too.
- ``cache`` — the SOURCE_HASH staleness discipline (target headers ⊕ ruleset).
- ``llm`` — generation plumbing: anthropic-SDK streaming (the default
  transport; ``generate_with_retries`` accepts any callable), target-header
  embedding for the user turn, code-fence stripping.

Library-side conventions the shims rely on are documented in
``docs/INTEROP.md`` (TRACKED_HERE forwarding, the single-default-argument
rule, ``opaque_at`` for barriers).
"""

from pathlib import Path

RULESET_PATH = Path(__file__).with_name("ruleset.txt")


def ruleset_text() -> str:
    """The packaged classification ruleset (feeds the SOURCE_HASH)."""
    return RULESET_PATH.read_text(encoding="utf-8")
