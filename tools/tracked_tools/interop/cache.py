"""``SOURCE_HASH`` staleness cache shared by all shim integrators.

An interop shim carries a ``// SOURCE_HASH: <hex>`` line whose value is
``sha256( sha256(target-header-bytes) ⊕ sha256(ruleset-text) )``.  A cached shim
is reused verbatim only when BOTH the target library's headers and the
generating integrator's ruleset are unchanged — a change to either forces
regeneration.  This module owns that computation and the read/stamp helpers; the
*ruleset text* is supplied by the caller (it is target-specific — the tracked
integrator's classification prompt, a future dd integrator's, etc.), which is
why :func:`compute_source_hash` takes it as a parameter rather than hashing a
module-level prompt.

Logic-identical port of the AMP consumer's ``agents/integrator_base/cache.py``
(itself lifted verbatim from its tracked integrator), so existing shims'
``SOURCE_HASH`` values are preserved byte-for-byte across the migration.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

# Files under the target-library header directory that participate in the
# SOURCE_HASH.  Kept broad on purpose: any C/C++ header flavor invalidates the
# cached shim when its bytes change.  Non-header files (README, etc.) are
# ignored so documentation churn does not force regeneration.
HEADER_SUFFIXES = {".h", ".hpp", ".hh", ".hxx", ".ipp", ".inc", ".cuh", ".tcc"}

SOURCE_HASH_RE = re.compile(r"//\s*SOURCE_HASH:\s*(\S+)")

# Written verbatim by a generator's placeholder; post-processing replaces it with
# the real hash.  Treated as "no hash" so it never counts as a cache hit.
SOURCE_HASH_PENDING = "PENDING"


def ruleset_hash(ruleset_text: str) -> str:
    """SHA-256 of an integrator's ruleset (its LLM system prompt).

    Folded into the shim's ``SOURCE_HASH`` so that a rule refinement (adding a
    classification rule, tightening one, etc.) invalidates every cached shim
    automatically.  Without this, a shim cached against an unchanged target-header
    tree would be reused verbatim even after the rules that generated it changed —
    silently serving a stale shim and defeating any re-validation.
    """
    return hashlib.sha256(ruleset_text.encode("utf-8")).hexdigest()


def compute_source_hash(headers_dir: Path, ruleset_text: str) -> str:
    """The shim's staleness key: target headers AND the ruleset version.

    Combining both means the cache hits only when BOTH the target library's
    headers and the generating ruleset are unchanged.  A change to either forces
    regeneration, so first-time integrations and rule refinements both invalidate
    correctly.
    """
    h = hashlib.sha256()
    h.update(hash_header_dir(headers_dir).encode("utf-8"))
    h.update(b"\0")
    h.update(ruleset_hash(ruleset_text).encode("utf-8"))
    return h.hexdigest()


def compute_region_hash(
    region_src: str,
    ruleset_text: str,
    scalar_type: str,
    writes: list[str],
) -> str:
    """The *regional* shim's staleness key (the ``integrate_region`` analogue of
    :func:`compute_source_hash`).

    A regional shim is cached against a single code region, not a header tree, so
    the hash folds in the region's own bytes (``region_src``), the generating
    integrator's ruleset (so a rule refinement invalidates every cached shim, as
    in the whole-app path), the extended scalar type it was generated for
    (``float-float`` vs ``double-double`` are different shims), and the Fix-C write
    set (a schema change to the region's writes invalidates the shim, since the
    boundary patch is synthesized from it).  Writes are sorted so the key is
    order-independent.
    """
    h = hashlib.sha256()
    h.update(region_src.encode("utf-8"))
    h.update(b"\0")
    h.update(ruleset_hash(ruleset_text).encode("utf-8"))
    h.update(b"\0")
    h.update(scalar_type.encode("utf-8"))
    h.update(b"\0")
    h.update("\0".join(sorted(writes)).encode("utf-8"))
    return h.hexdigest()


def hash_header_dir(headers_dir: Path) -> str:
    """SHA-256 over the header files under ``headers_dir`` (recursive).

    The digest folds in each header's path relative to ``headers_dir`` and its
    bytes, walked in sorted order, so a rename, move, edit, add, or delete of any
    header changes the hash.  Non-header files (see :data:`HEADER_SUFFIXES`) are
    skipped so documentation churn does not invalidate the cached shim.
    """
    h = hashlib.sha256()
    files = sorted(
        p for p in headers_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in HEADER_SUFFIXES
    )
    for path in files:
        rel = path.relative_to(headers_dir).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def extract_source_hash(text: str) -> str | None:
    """Return the hash on the ``// SOURCE_HASH:`` line, or ``None`` if absent.

    A ``PENDING`` placeholder is treated as "no hash" so it never counts as a
    cache hit.
    """
    m = SOURCE_HASH_RE.search(text)
    if not m:
        return None
    value = m.group(1)
    return None if value == SOURCE_HASH_PENDING else value


def apply_source_hash(text: str, source_hash: str) -> str:
    """Stamp the real hash onto the shim's ``// SOURCE_HASH:`` line.

    Replaces a ``PENDING`` placeholder (or any prior value) with ``source_hash``.
    If the generator omitted the line entirely, inject one after the first line so
    the caching contract still holds on the next run.
    """
    if SOURCE_HASH_RE.search(text):
        return SOURCE_HASH_RE.sub(f"// SOURCE_HASH: {source_hash}", text, count=1)
    lines = text.splitlines()
    insert_at = 1 if lines else 0
    lines.insert(insert_at, f"// SOURCE_HASH: {source_hash}")
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
