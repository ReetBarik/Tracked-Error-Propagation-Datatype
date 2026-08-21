"""LLM generation plumbing shared by shim integrators.

Three concerns, all target-agnostic:

* :func:`stream_llm` — the Anthropic-SDK *streaming* call (a full shim can
  approach the token cap, and the SDK refuses a non-streaming request whose
  worst-case duration exceeds 10 minutes), plus :func:`strip_code_fences`.
* target-header embedding for the user turn (:func:`collect_target_headers`,
  :func:`embed_file`, …) — split the target tree into the driver's transitive
  include-closure (embedded in full) and the rest (listed by name).
* :func:`generate_with_retries` — a bounded generate→accept loop that bypasses
  any cache between attempts, for the classification-stability pattern (e.g.
  re-generate until a shim classifies a sign helper the intended way).  The
  LLM call itself is a pluggable seam: ``generate_fn`` is any callable, so an
  integrator may use :func:`stream_llm` (anthropic SDK) or its own transport.

The system prompt / ruleset is passed in by the caller — it is target-specific.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Iterable

# The embedding closure walks the same header flavors the cache hashes.
from tracked_tools.interop.cache import HEADER_SUFFIXES as _EMBED_HEADER_SUFFIXES

# Per-file char cap when embedding a header's contents in the user message, so a
# single pathological header can't blow the context budget.
HEADER_EMBED_CAP = 60000

INCLUDE_RE = re.compile(r'#\s*include\s*[<"]([^">]+)[">]')


# ---------------------------------------------------------------------------
# Anthropic streaming shim
# ---------------------------------------------------------------------------

def stream_llm(system_prompt: str, user_message: str, cfg, max_tokens: int) -> str:
    """Call the LLM (``cfg.model``, via the anthropic SDK) and return its text.

    Imported lazily so importing this module never requires the anthropic client
    or a live endpoint.  Streams because a full shim can approach ``max_tokens``
    and the SDK refuses a non-streaming request whose worst-case duration exceeds
    10 minutes.
    """
    import anthropic

    client = anthropic.Anthropic(base_url=cfg.base_url, api_key=cfg.auth_token)
    with client.messages.stream(
        model=cfg.model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        final = stream.get_final_message()

    text = "".join(
        block.text for block in final.content
        if getattr(block, "type", None) == "text"
    ).strip()
    if not text:
        raise RuntimeError("integrator_base: LLM returned no text content")
    return strip_code_fences(text)


def stream_shim(system_prompt: str, user_message: str, cfg, max_tokens: int) -> tuple[str, int]:
    """Like :func:`stream_llm`, but also return the call's total token count.

    The regional integrators report ``llm_tokens`` on their
    :class:`~agents.integrator_base.region.RegionIntegrationResult` (the Patcher
    accumulates them into the Strategy budget), so they need the usage the plain
    :func:`stream_llm` discards.  Returns ``(text, input_tokens + output_tokens)``.
    """
    import anthropic

    client = anthropic.Anthropic(base_url=cfg.base_url, api_key=cfg.auth_token)
    with client.messages.stream(
        model=cfg.model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        final = stream.get_final_message()

    text = "".join(
        block.text for block in final.content
        if getattr(block, "type", None) == "text"
    ).strip()
    if not text:
        raise RuntimeError("integrator_base: LLM returned no text content")

    usage = getattr(final, "usage", None)
    tokens = 0
    if usage is not None:
        tokens = (getattr(usage, "input_tokens", 0) or 0) + \
                 (getattr(usage, "output_tokens", 0) or 0)
    return strip_code_fences(text), tokens


def strip_code_fences(text: str) -> str:
    """Strip a leading/trailing markdown code fence if the model added one."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    # Drop the opening fence line (``` or ```cpp) and a trailing fence line.
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Bounded retry loop with cache-bypass
# ---------------------------------------------------------------------------

def generate_with_retries(
    generate_fn: Callable[[int], str],
    accept_fn: Callable[[str], bool],
    *,
    max_attempts: int = 5,
) -> str:
    """Call ``generate_fn`` until ``accept_fn`` approves, up to ``max_attempts``.

    The classification-stability pattern from Stage 1: whole-app shim generation
    can occasionally misclassify a boundary function (the canonical case being a
    sign helper classified ``-> int`` instead of ``-> Tracked``), which cascades
    into many type errors.  A bounded retry with the cache bypassed accepts the
    first generation that passes ``accept_fn``.

    ``generate_fn(attempt_index)`` produces a fresh candidate (the index lets a
    caller vary the prompt/temperature per attempt if it wishes); it is the
    caller's responsibility to bypass any shim cache — this loop always calls
    ``generate_fn`` afresh and never consults a cache.  ``accept_fn(candidate)``
    returns True to accept.  Returns the first accepted candidate, or the last
    one produced if none passed within budget (callers can re-check / surface it).
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    last = ""
    for attempt in range(max_attempts):
        last = generate_fn(attempt)
        if accept_fn(last):
            return last
    return last


# ---------------------------------------------------------------------------
# Target-header embedding for the user turn
# ---------------------------------------------------------------------------

def collect_target_headers(
    headers_dir: Path, driver_text: str
) -> tuple[list[Path], list[Path]]:
    """Split the target header tree into (driver include-closure, everything else).

    Starting from the driver's local ``#include`` lines, BFS over local includes
    that resolve inside ``headers_dir``.  System includes (``<Kokkos_Core.hpp>``,
    ``<cmath>``, ``<tracked/...>``) never resolve here, so they are naturally
    skipped.  Returns both lists sorted by path for deterministic prompts.
    """
    all_headers = {
        p.resolve()
        for p in headers_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in _EMBED_HEADER_SUFFIXES
    }

    seen: set[Path] = set()
    queue: list[Path] = []

    def _seed(text: str) -> None:
        for inc in INCLUDE_RE.findall(text):
            resolved = resolve_local_include(inc, headers_dir)
            if resolved is not None and resolved not in seen:
                queue.append(resolved)

    _seed(driver_text)
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        try:
            _seed(current.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass

    closure = sorted(seen)
    others = sorted(all_headers - seen)
    return closure, others


def resolve_local_include(inc: str, headers_dir: Path) -> Path | None:
    """Resolve an ``#include`` target to a file under ``headers_dir``, or None.

    Tries the path as written relative to ``headers_dir`` first, then falls back
    to a basename match anywhere in the tree (a CMake include path often adds both
    the tree root and a subdir, so ``#include "B2m.h"`` and ``#include
    "box/B2m.h"`` both need to resolve).
    """
    candidate = (headers_dir / inc)
    if candidate.is_file():
        return candidate.resolve()
    base = Path(inc).name
    matches = sorted(p for p in headers_dir.rglob(base) if p.is_file())
    return matches[0].resolve() if matches else None


def embed_file(path: Path, label: str, text: str | None = None) -> str:
    """Render one file as a fenced ``### label`` section, capped in length."""
    if text is None:
        text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > HEADER_EMBED_CAP:
        text = (
            text[:HEADER_EMBED_CAP]
            + f"\n// ... [truncated {len(text) - HEADER_EMBED_CAP} chars] ...\n"
        )
    return f"### `{label}`\n```cpp\n{text}\n```\n"


def rel(path: Path, headers_dir: Path) -> str:
    try:
        return path.resolve().relative_to(headers_dir.resolve()).as_posix()
    except ValueError:
        return path.name


