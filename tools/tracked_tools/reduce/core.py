"""Journal -> mergeable code-region stability report (the ``tracked-reduce`` core).

This is the *map* step of a sharded characterization: each shard runs a tracked
binary over a disjoint slice of the input space, then reduces its own (possibly
enormous, transient) ``journal.jsonl`` **in-process** to a small, mergeable
report.  A separate *merge* step combines the per-shard reports into a single
consolidated report.  The journal itself is never moved or concatenated — only
the reductions are (per-chunk metadata reduces cleanly across chunks).

Two things this computes that a flat per-line rollup cannot:

* **Forward cone / amplification.**  Downcast safety is a *forward* dataflow
  property: raising a value's error floor to a narrower format's unit roundoff
  is safe only if every path from it to an observable output attenuates the
  injected error below the acceptance margin.  We build the per-sample DAG from
  each record's ``in`` operand edges, invert them, and run one backward pass
  computing, for every node at once, ``amp(v) = max over consumers c of
  cond(c) * amp(c)`` (``amp = 1`` at output sinks).  ``amp`` is a conservative
  upper bound (it ignores the ``max``-gating in the real error recurrence, so
  it can over-flag danger — the safe direction for a downcast guard).

* **Value-range guard.**  A narrower format has a narrower exponent range; a
  well-conditioned value that underflows/overflows it is unsafe to downcast for
  a reason the error model doesn't see.  We track min/max ``|val|`` per region
  from the recorded ``val``.

Signal classes are *mechanistic* descriptions of the error phenomenon (three
distinct failure modes + a stable class + the documented saturation cap), not
remediations.  (The saturation class is spelled ``atan2_saturation`` — a
historical name kept for downstream readers; the 1/u cap is in fact emitted by
log/sin/cos/atan2 and add/sub underflow alike.)  The reducer is policy-neutral
and emits measurements only
(``max_amp``, ``predicted_rel_err_if_<fmt>``, ``value_range_ok_for_<fmt>``,
class).  Applying an acceptance margin and choosing an action is the consumer's
job, so one characterization run serves any acceptance policy.

**Policy seam** (:class:`ReducerConfig`): the mechanistic thresholds, the
saturation cap, the set of target formats to predict for (``predictions``:
format name -> unit roundoff), and the value-range guard bounds are all
injectable.  The defaults describe IEEE single precision — the universal
downcast target — and consumers with additional formats (e.g. the AMP
pipeline's float-float emulation) extend ``predictions`` with their own
constants.  Everything else in the report is measured.

Scope grammar note: the per-sample scope suffix is ``@<key>=<value>[/...]``
appended after the ``#<callsite-counter>`` of a v0.3+ id.  The unit bucket key
is spelled ``integral=`` (a historical name from the first production consumer,
kept because journals and downstream readers depend on it); ``sample=`` is the
sample index; ``line=<file>:<N>`` marks injected statement regions and is
excluded from the sample identity.

The reducer targets the v0.3 journal schema (records carry ``id``, ``in``,
``prov_vars``/``prov_consts``).  Older journals without ``id`` cannot supply
the DAG; they degrade to per-location aggregation with no forward-cone signal
(counted in ``no_id_records``).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

# ---------------------------------------------------------------------------
# Numeric constants (library-owned; IEEE formats are not consumer policy)
# ---------------------------------------------------------------------------

U_DOUBLE = 2.0 ** -53          # IEEE double unit roundoff, ~1.11e-16
U_FLOAT = 2.0 ** -24           # IEEE single unit roundoff, ~5.96e-8
FLT_MIN_NORMAL = 1.1754943508222875e-38
FLT_MAX = 3.4028234663852886e38
# The library's documented 1/u saturation cap (ops.hpp / tracked.hpp): emitted
# for log/sin/cos/atan2 near their poles and for add/sub underflow-to-zero.
SATURATION_CAP = 2.0 ** 53


def _default_predictions() -> dict[str, float]:
    return {"float": U_FLOAT}


@dataclass
class ReducerConfig:
    """Thresholds + policy seam for the reduction.

    The threshold fields describe the KIND of numerical phenomenon
    (ill-conditioned op, cancellation cascade, local cancellation) — properties
    of the error mechanism, not of any acceptance policy.  The seam fields
    (``predictions``, ``range_*``) name the downcast target formats a consumer
    cares about; defaults describe IEEE single.
    """

    local_cancel_cond: float = 1e15      # cond>this (post gate-a) => local cancellation
    high_cond: float = 1e6               # cond in [high_cond, local_cancel) => log-near-root
    cascade_rel_err: float = 1e-6        # rel_err>this with low cond => cancellation cascade
    cascade_cond_ceiling: float = 1e3    # "low per-op cond" ceiling for cascade detection
    gate_a_rel_tol: float = 1e-9         # |cond - cap| / cap tolerance for gate-(a)
    # An add/sub op is a *cancellation contributor* to a cascade chain when its
    # operands nearly cancel: |a-b| / (|a|+|b|) < this.  0.1 (~1 lost decimal
    # digit) is the starter bar; it is the reciprocal of the op's own condition
    # number, so the val-based test and a cond>1/ratio fallback agree.
    cascade_cancel_ratio: float = 0.1
    # The library's 1/u cap value (gate-a); override only for non-double T.
    saturation_cap: float = SATURATION_CAP
    # Policy seam: target formats to emit predicted_rel_err_if_<name> fields
    # for, as {name: unit roundoff}.  The prediction is u_fmt * cond * amp —
    # the rel-error that would reach an output if the region were computed in
    # that format.
    predictions: dict[str, float] = field(default_factory=_default_predictions)
    # Policy seam: value-range guard target format (name + normal range).
    # Set range bounds to None to disable a side of the guard.
    range_name: str = "float"
    range_min_normal: float | None = FLT_MIN_NORMAL
    range_max: float | None = FLT_MAX

    @property
    def range_ok_key(self) -> str:
        return f"value_range_ok_for_{self.range_name}"


# v2 (2026-07-22): every region record and cascade-chain record gains an
# explicit ``integral`` field carrying its parent ``integrals[<name>]`` bucket
# name.  Purely additive/self-describing — the report was ALREADY per-integral
# (top-level bucketing), so this only stamps the bucket name onto each record so
# a downstream consumer can key on (line, integral) without re-deriving it from
# the enclosing bucket.  No structural change; v1 readers ignore the new field.
SCHEMA_VERSION = 2


# ---------------------------------------------------------------------------
# Mergeable log10 histogram (approximate, exactly-additive percentiles)
# ---------------------------------------------------------------------------

class LogHist:
    """Sparse base-10 log histogram over positive values.

    Bucketed by ``floor(log10(x))`` (one decade per bucket).  Bucket counts are
    exactly additive, so a percentile read from the merged histogram equals the
    percentile of the concatenated sample set (to one-decade resolution).  This
    is the mergeable quantile sketch a p99 acceptance metric needs — you cannot
    average shard percentiles.
    """

    __slots__ = ("buckets", "total")

    def __init__(self) -> None:
        self.buckets: dict[int, int] = {}
        self.total: int = 0

    def add(self, x: float) -> None:
        if x is None or not math.isfinite(x) or x <= 0.0:
            return
        b = math.floor(math.log10(x))
        self.buckets[b] = self.buckets.get(b, 0) + 1
        self.total += 1

    def merge(self, other: "LogHist") -> None:
        for b, c in other.buckets.items():
            self.buckets[b] = self.buckets.get(b, 0) + c
        self.total += other.total

    def quantile(self, q: float) -> float | None:
        """Approximate q-quantile as the lower edge of the crossing decade."""
        if self.total == 0:
            return None
        target = q * self.total
        cum = 0
        for b in sorted(self.buckets):
            cum += self.buckets[b]
            if cum >= target:
                return 10.0 ** b
        return 10.0 ** max(self.buckets)

    def to_dict(self) -> dict:
        return {"buckets": {str(k): v for k, v in self.buckets.items()},
                "total": self.total}

    @classmethod
    def from_dict(cls, d: dict) -> "LogHist":
        h = cls()
        h.buckets = {int(k): v for k, v in d.get("buckets", {}).items()}
        h.total = d.get("total", sum(h.buckets.values()))
        return h


# ---------------------------------------------------------------------------
# Record / scope parsing
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path) -> Iterator[dict]:
    with Path(path).open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Bad JSON on line {lineno} of {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Version-aware journal reading (docs/SCHEMA.md, normative)
# ---------------------------------------------------------------------------

# Legacy emitters clamped ±Inf to ±DBL_MAX; v1 non-finite sentinels are mapped
# to the same magnitude (alarm direction), so the SAME computation journaled
# as v0.3 or v1 reduces identically for ±Inf.  NaN — which the legacy pipeline
# silently zeroed ("maximally stable", the documented bug) — now also clamps
# to the alarm direction in v1 mode (a documented, deliberate divergence).
DBL_MAX_CLAMP = 1.7976931348623157e308

_SENTINELS = ("nan", "inf", "-inf")
_NUMERIC_KEYS = ("val", "cond", "rel_err")


def _map_v1_record(rec: dict, path) -> dict:
    """Apply the v1 reader rules to one op record (SCHEMA.md 'Non-finite
    encoding'): exact sentinels only, no nulls, alarm-direction mapping."""
    nonfinite = False
    for k in _NUMERIC_KEYS:
        if k not in rec:
            continue
        v = rec[k]
        if v is None:
            raise ValueError(
                f"{path}: null {k!r} in a v1 journal — legacy contamination "
                "(legacy and v1 shards must not be mixed)")
        if isinstance(v, str):
            if v not in _SENTINELS:
                raise ValueError(
                    f"{path}: invalid value {v!r} for {k!r} — v1 accepts "
                    "exactly the sentinels \"nan\"/\"inf\"/\"-inf\"")
            rec[k] = -DBL_MAX_CLAMP if v == "-inf" else DBL_MAX_CLAMP
            nonfinite = True
    if nonfinite:
        rec["_nonfinite"] = True
    return rec


def read_journal(path, legacy: bool = False) -> Iterator[dict]:
    """Stream op records from a journal, enforcing the SCHEMA.md reader rules.

    v1 mode (default): line 1 MUST be a header record with ``schema == 1``;
    mid-stream headers (shard boundaries from concatenation/streaming) are
    validated and skipped BEFORE any grouping; non-finite sentinels are mapped
    per :func:`_map_v1_record`; ``null`` and non-sentinel strings hard-fail.

    Legacy mode (explicit opt-in for pre-v1 journals): byte-identical behavior
    to the historical reducer, except any record carrying a ``schema`` key
    hard-fails (v1 contamination).
    """
    it = _read_jsonl(Path(path))
    if legacy:
        for rec in it:
            if "schema" in rec:
                raise ValueError(
                    f"{path}: v1 header record found in a legacy-mode read — "
                    "this journal is not pre-v1")
            yield rec
        return

    first = next(it, None)
    if first is None:
        return                                # zero-byte file: empty journal
    if "schema" not in first:
        raise ValueError(
            f"{path}: no v1 header on line 1 — a pre-v1 (v0.3) journal must "
            "be read in explicit legacy mode (--legacy / legacy=True)")
    if first.get("schema") != 1:
        raise ValueError(
            f"{path}: unsupported journal schema {first.get('schema')!r}")
    for rec in it:
        if "schema" in rec:                   # mid-stream shard boundary
            if rec.get("schema") != 1:
                raise ValueError(
                    f"{path}: mixed journal schemas mid-stream "
                    f"({rec.get('schema')!r} after 1)")
            continue
        yield _map_v1_record(rec, path)


def _scope_str(node_id: str) -> str:
    """Extract the scope suffix from an op id, or "" if unscoped.

    Ids look like ``<op>@<file>:<line>#<counter>[@<scope>]``.  The scope is
    appended after the ``#<counter>``, so we split on the first ``#`` (the file
    part never contains one) and take whatever follows the next ``@``.
    """
    if not node_id:
        return ""
    hash_idx = node_id.find("#")
    if hash_idx == -1:
        return ""
    rest = node_id[hash_idx + 1:]
    at = rest.find("@")
    return rest[at + 1:] if at != -1 else ""


def _parse_scope(scope: str) -> dict[str, str]:
    """``"integral=B15/sample=42"`` -> ``{"integral": "B15", "sample": "42"}``."""
    out: dict[str, str] = {}
    for part in scope.split("/"):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k] = v
    return out


def _prov_vars(rec: dict) -> list[str]:
    """Source-variable provenance, tolerant of schema drift.

    v0.3 splits provenance into ``prov_vars`` (source roots) + ``prov_consts``
    (named constants).  Older journals used a single flat ``prov``.
    """
    if "prov_vars" in rec:
        return list(rec.get("prov_vars") or [])
    return list(rec.get("prov") or rec.get("provenance") or [])


def _region_local_reads(rec: dict, source_ids: set[str],
                        prov_var_names: set[str]) -> list[str]:
    """Source variables *read directly* in the record's code region.

    ``prov_vars`` is a transitive provenance union (every source root that flows
    into the value, computed on any earlier line), which is the wrong input for
    a *regional* precision promotion — it names whole-app inputs seeded far
    upstream.  The region-local set we CAN recover from the journal is the
    source variables the region's ops read as **direct leaf operands** (``in``
    ids that have no producing record and appear in some record's
    ``prov_vars``): the named inputs textually used at this source line.  It is
    by construction a subset of ``prov_vars``.

    Caveat: the journal has no LHS/assignment-target field and ``track()``
    emits no record, so the *declared/assigned* (written) variable of a region
    is not nameable.  This is the tightest region-scoped *named* variable set
    the data supports (region-local reads), not the write set.
    """
    return [o for o in rec.get("in", [])
            if o in source_ids and o in prov_var_names]


def _prov_all(rec: dict) -> list[str]:
    if "prov_vars" in rec or "prov_consts" in rec:
        return list(rec.get("prov_vars") or []) + list(rec.get("prov_consts") or [])
    return list(rec.get("prov") or rec.get("provenance") or [])


def _cond(rec: dict) -> float:
    try:
        c = float(rec.get("cond", 0.0))
    except (TypeError, ValueError):
        return 0.0
    return c if math.isfinite(c) and c > 0.0 else 0.0


def _is_gate_a(cond: float, cfg: ReducerConfig) -> bool:
    """True for the documented library saturation cap at 1/u (gate-a)."""
    if cond <= 0.0:
        return False
    return abs(cond - cfg.saturation_cap) <= cfg.gate_a_rel_tol * cfg.saturation_cap


def _sample_key(scope_str: str) -> str:
    """The sample identity: the scope MINUS any ``line=`` component.

    A ``line=<file:line>`` sub-scope (pushed around a source statement to make
    it a code region) changes the id suffix op-to-op *within* one sample.
    Sample grouping and the whole-sample DAG must ignore it, or the
    accumulation's operands (computed on earlier lines, outside the line scope)
    would be split into a different batch and mis-read as sources.
    """
    d = _parse_scope(scope_str)
    return "/".join(f"{k}={v}" for k, v in d.items() if k != "line")


def _region_key(rec: dict) -> str:
    """The code-region a record belongs to: ``line=`` scope, else ``at``.

    ``line=`` (injected as a scope, so it lands on operator ops too) is the
    primary code-region signal for operator-heavy libraries; ``at`` (a real
    ``file:fn:line`` from a located named call) is used when present; ""
    otherwise (unattributed — the operator arithmetic with no line scope).
    """
    line = _parse_scope(_scope_str(rec.get("id", ""))).get("line")
    return line or rec.get("at", "") or ""


def _iter_samples(records: Iterable[dict]) -> Iterator[tuple[str, list[dict]]]:
    """Group a stream of records into contiguous per-sample batches.

    A conforming driver emits each ``(unit, sample)`` fully before the next
    (RAII scope, serial per-thread execution, append-ordered flush — the
    chunkable-driver contract), so grouping runs of equal *sample key* recovers
    per-sample batches without loading the whole journal.  Grouping is on the
    sample key (scope minus ``line=``) so a line sub-scope pushed mid-sample
    does not fragment the batch.
    """
    cur_key: str | None = None
    batch: list[dict] = []
    for rec in records:
        key = _sample_key(_scope_str(rec.get("id", "")))
        if cur_key is None:
            cur_key = key
        if key != cur_key:
            yield cur_key, batch
            batch = []
            cur_key = key
        batch.append(rec)
    if batch:
        yield cur_key or "", batch


# ---------------------------------------------------------------------------
# Per-sample DAG + forward-cone amplification
# ---------------------------------------------------------------------------

def _topo_order(nodes: dict[str, dict]) -> list[str]:
    """Dependency-topological order (operands before the ops that consume them).

    Iterative DFS post-order over the internal ``in`` edges; robust to the
    per-sample DAG being a forest and to deep cascade chains (no recursion).
    """
    visited: set[str] = set()
    order: list[str] = []
    for root in nodes:
        if root in visited:
            continue
        stack: list[tuple[str, bool]] = [(root, False)]
        while stack:
            node, processed = stack.pop()
            if processed:
                order.append(node)
                continue
            if node in visited:
                continue
            visited.add(node)
            stack.append((node, True))
            for o in nodes[node].get("in", []):
                if o in nodes and o not in visited:
                    stack.append((o, False))
    return order


def _analyze_sample(records: list[dict], cfg: ReducerConfig):
    """Build the sample DAG and compute amplification for every node.

    Returns ``(nodes, amp, node_sens, source_sens, source_ids)`` where:
      * ``amp[v]``        forward amplification of an error at v to any output
      * ``node_sens[v]``  downcast impact factor = cond(v) * amp(v) for op v
      * ``source_sens[s]``downcast impact factor of a record-less source id s
      * ``source_ids``    operand ids with no record (track vars / consts / lits)

    Nodes without an ``id`` (v0.2 journals) yield an empty analysis — the caller
    falls back to per-location aggregation only.
    """
    nodes: dict[str, dict] = {}
    for r in records:
        rid = r.get("id")
        if rid is not None:
            nodes[rid] = r
    if not nodes:
        return {}, {}, {}, {}, set()

    children: dict[str, list[str]] = {rid: [] for rid in nodes}
    source_ids: set[str] = set()
    for rid, r in nodes.items():
        for o in r.get("in", []):
            if o in nodes:
                children[o].append(rid)
            else:
                source_ids.add(o)

    order = _topo_order(nodes)

    # Backward amplification pass: consumers before the node (reversed topo).
    amp: dict[str, float] = {}
    for v in reversed(order):
        ch = children[v]
        if not ch:
            amp[v] = 1.0                     # output sink (no internal consumer)
        else:
            best = 0.0
            for c in ch:
                cand = _cond_eff(nodes[c]) * amp[c]
                if cand > best:
                    best = cand
            amp[v] = best if best > 0.0 else 1.0

    node_sens: dict[str, float] = {}
    source_sens: dict[str, float] = {}
    for rid, r in nodes.items():
        impact = _cond_eff(r) * amp[rid]
        node_sens[rid] = impact
        for o in r.get("in", []):
            if o in source_ids:
                # A downcast floor at source o reaches r as cond(r)*u, then amp(r).
                if impact > source_sens.get(o, 0.0):
                    source_sens[o] = impact
    return nodes, amp, node_sens, source_sens, source_ids


def _cond_eff(rec: dict) -> float:
    """Effective local cond for amplification: a real cond, or 1 as a floor.

    Ops with cond <= 0 recorded (mul/div use cond=1; some emit 0) still pass
    error through, so a unit floor keeps them in the amplification chain rather
    than zeroing a downstream cone.  gate-(a) saturation is left as-is here (it
    genuinely amplifies) — it is only excluded from the *reported* max_cond.
    """
    c = _cond(rec)
    return c if c > 0.0 else 1.0


# ---------------------------------------------------------------------------
# Cascade-chain localization (per sample, backward over the value DAG)
# ---------------------------------------------------------------------------
#
# A cancellation *cascade* is accumulated error: the final value carries a large
# rel_err even though no single op is ill-conditioned (each per-op cond is low).
# A single-span region is the wrong shape for it — the error is a property of a
# *chain* of near-equal add/sub ops that can span many source lines.  So we walk
# the value DAG backward from each cascade *victim* (a final value = DAG sink
# with high rel_err + low per-op cond), collect the add/sub ancestors whose
# operands nearly cancel, and emit ONE ``cascade_chain`` record per victim with
# the union of their source lines.  Chains that share a line are NOT merged here
# — the consumer resolves per-line overlap.

_ADD_SUB = ("add", "sub")


def _rel_err(rec: dict) -> float:
    try:
        r = float(rec.get("rel_err", 0.0))
    except (TypeError, ValueError):
        return 0.0
    return r if math.isfinite(r) and r > 0.0 else 0.0


def _cancellation_ratio(rec: dict, nodes: dict[str, dict]) -> float | None:
    """``|a-b| / (|a|+|b|)`` for a binary add/sub op, or None if unknowable.

    The op's own ``val`` is the result magnitude ``|a±b|``; operand magnitudes
    come from the producing records.  A leaf operand (source var/const/literal)
    has no journaled value, so if either operand's magnitude is unavailable this
    returns None and the caller falls back to the recorded cond (which, for
    add/sub, is definitionally ``(|a|+|b|)/|result|`` = the reciprocal ratio).
    """
    ins = rec.get("in", [])
    if len(ins) != 2:
        return None
    denom = 0.0
    for o in ins:
        src = nodes.get(o)
        if src is None:
            return None
        try:
            denom += abs(float(src.get("val", 0.0)))
        except (TypeError, ValueError):
            return None
    if denom <= 0.0 or not math.isfinite(denom):
        return None
    try:
        result = abs(float(rec.get("val", 0.0)))
    except (TypeError, ValueError):
        return None
    return result / denom


def _is_cascade_contributor(rec: dict, nodes: dict[str, dict],
                            cfg: ReducerConfig) -> bool:
    """True for an add/sub op whose operands nearly cancel (chain contributor)."""
    if rec.get("op") not in _ADD_SUB:
        return False
    ratio = _cancellation_ratio(rec, nodes)
    if ratio is not None:
        return ratio < cfg.cascade_cancel_ratio
    # val-based ratio unavailable (leaf operand): fall back to the op's own cond,
    # which for add/sub equals (|a|+|b|)/|result| = 1/ratio.
    cond = _cond(rec)
    if cond <= 0.0 or _is_gate_a(cond, cfg):
        return False
    return (1.0 / cond) < cfg.cascade_cancel_ratio


def _cascade_victims(nodes: dict[str, dict], children: dict[str, list[str]],
                     cfg: ReducerConfig) -> list[str]:
    """Final values (DAG sinks) with high rel_err + low per-op cond."""
    victims = []
    for rid, r in nodes.items():
        if children.get(rid):            # has an internal consumer → not final
            continue
        cond = _cond(r)
        if _is_gate_a(cond, cfg):
            continue
        if _rel_err(r) >= cfg.cascade_rel_err and cond < cfg.cascade_cond_ceiling:
            victims.append(rid)
    return victims


def _ancestors(victim_id: str, nodes: dict[str, dict]) -> set[str]:
    """All internal-node ancestors of ``victim_id`` via ``in`` edges (iterative)."""
    seen: set[str] = set()
    stack = [victim_id]
    while stack:
        nid = stack.pop()
        for o in nodes[nid].get("in", []):
            if o in nodes and o not in seen:
                seen.add(o)
                stack.append(o)
    return seen


def _short_hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]


def _parse_region_span(region_key: str) -> dict | None:
    """``"B2m.h:355"`` -> ``{"file": "B2m.h", "line_start": 355, "line_end": 355}``.

    Returns None for a non-localizable key (empty, or no numeric line).
    """
    if not region_key or ":" not in region_key:
        return None
    file, _, line = region_key.rpartition(":")
    if not file or not line.isdigit():
        return None
    return {"file": file, "line_start": int(line), "line_end": int(line)}


def _prediction_fields(sens: float, cfg: ReducerConfig) -> dict[str, float]:
    """``predicted_rel_err_if_<fmt>`` fields for every configured target format.

    The prediction is a *measured* quantity: ``u_fmt * cond * amp`` — the
    rel-error that would reach an output if this region were computed in the
    format — using only the format constant and the measured forward-cone
    amplification.  The consumer compares it to ITS margin.
    """
    return {f"predicted_rel_err_if_{name}": u * sens
            for name, u in cfg.predictions.items()}


def _extract_cascade_chains(nodes: dict[str, dict], amp: dict[str, float],
                            node_sens: dict[str, float], source_ids: set[str],
                            prov_var_names: set[str], integral: str,
                            sample_key: str, cfg: ReducerConfig) -> dict[str, dict]:
    """One ``cascade_chain`` record per localizable victim, keyed by chain_id."""
    children: dict[str, list[str]] = {rid: [] for rid in nodes}
    for rid, r in nodes.items():
        for o in r.get("in", []):
            if o in nodes:
                children[o].append(rid)

    chains: dict[str, dict] = {}
    for victim in _cascade_victims(nodes, children, cfg):
        candidates = _ancestors(victim, nodes)
        candidates.add(victim)
        contributors = [c for c in candidates
                        if _is_cascade_contributor(nodes[c], nodes, cfg)]
        if not contributors:
            continue

        # union of contributing source lines (skip unlocalizable contributors)
        spans: dict[tuple, dict] = {}
        for c in contributors:
            span = _parse_region_span(_region_key(nodes[c]))
            if span is not None:
                spans[(span["file"], span["line_start"], span["line_end"])] = span
        if not spans:
            continue                       # no contributor carries a source line

        local_vars: set[str] = set()
        max_cond = 0.0
        max_sens = 0.0
        ops: dict[str, int] = {}
        for c in contributors:
            rc = nodes[c]
            local_vars.update(_region_local_reads(rc, source_ids, prov_var_names))
            cond = _cond(rc)
            if not _is_gate_a(cond, cfg) and cond > max_cond:
                max_cond = cond
            if node_sens.get(c, 0.0) > max_sens:
                max_sens = node_sens[c]
            ops[rc.get("op", "unknown")] = ops.get(rc.get("op", "unknown"), 0) + 1
        max_sens = max(max_sens, node_sens.get(victim, 0.0))

        chain_id = (f"cascade_{integral}_{_short_hash(sample_key)}"
                    f"_{_short_hash(victim)}")
        chains[chain_id] = {
            "kind": "cascade_chain",
            "chain_id": chain_id,
            # explicit parent-integral tag (v2).  Already implicit in chain_id
            # (``cascade_<integral>_...``); stamped here for symmetry with the
            # region records so a consumer never has to parse it back out.
            "integral": integral,
            "chain": [spans[k] for k in sorted(spans)],
            "signal_class": "cancellation_cascade",
            "non_localizable": False,
            "max_rel_err": _rel_err(nodes[victim]),
            "max_cond": max_cond,
            "max_sensitivity": max_sens,
            **_prediction_fields(max_sens, cfg),
            "n": len(contributors),
            "ops": ops,
            "region_local_vars": sorted(local_vars),
        }
    return chains


# ---------------------------------------------------------------------------
# Aggregation (the mergeable shard report)
# ---------------------------------------------------------------------------

def _new_region() -> dict:
    return {
        "ops": {},
        "n": 0,
        "max_cond": 0.0,
        "gate_a_count": 0,
        "max_rel_err": 0.0,
        "rel_err_hist": LogHist(),
        "max_sensitivity": 0.0,   # max cond*amp over ops at this location
        "max_amp": 0.0,
        "abs_val_min": None,
        "abs_val_max": None,
        "prov_vars": set(),
        "region_local_vars": set(),
    }


def _update_region(reg: dict, rec: dict, amp_v: float, sens_v: float,
                   cfg: ReducerConfig, local_vars: Iterable[str] = ()) -> None:
    reg["n"] += 1
    op = rec.get("op", "unknown")
    reg["ops"][op] = reg["ops"].get(op, 0) + 1

    cond = _cond(rec)
    if _is_gate_a(cond, cfg):
        reg["gate_a_count"] += 1
    elif cond > reg["max_cond"]:
        reg["max_cond"] = cond

    try:
        rel = float(rec.get("rel_err", 0.0))
    except (TypeError, ValueError):
        rel = 0.0
    if math.isfinite(rel) and rel > 0.0:
        if rel > reg["max_rel_err"]:
            reg["max_rel_err"] = rel
        reg["rel_err_hist"].add(rel)

    if sens_v > reg["max_sensitivity"]:
        reg["max_sensitivity"] = sens_v
    if amp_v > reg["max_amp"]:
        reg["max_amp"] = amp_v

    try:
        val = abs(float(rec.get("val", 0.0)))
    except (TypeError, ValueError):
        val = 0.0
    if math.isfinite(val) and val > 0.0:
        if reg["abs_val_min"] is None or val < reg["abs_val_min"]:
            reg["abs_val_min"] = val
        if reg["abs_val_max"] is None or val > reg["abs_val_max"]:
            reg["abs_val_max"] = val

    reg["prov_vars"].update(_prov_vars(rec))
    reg["region_local_vars"].update(local_vars)


def reduce_journal(path, cfg: ReducerConfig | None = None, *,
                   legacy: bool = False) -> dict:
    """Reduce one journal file to a mergeable shard report (streaming).

    ``legacy=True`` reads a pre-v1 (headerless v0.3) journal; the default
    hard-requires the v1 header (see :func:`read_journal`).  The v1-mode shard
    report additionally carries ``nonfinite_records`` — the count of records
    whose val/cond/rel_err was a non-finite sentinel, clamped to the alarm
    direction (the key is absent in legacy mode, whose output stays
    byte-identical to the historical reducer).
    """
    cfg = cfg or ReducerConfig()
    integrals: dict[str, dict] = {}
    samples_seen: dict[str, int] = {}
    no_id_records = 0
    nonfinite_records = 0

    for scope, batch in _iter_samples(read_journal(path, legacy=legacy)):
        for r in batch:
            if r.pop("_nonfinite", False):
                nonfinite_records += 1
        integral = _parse_scope(scope).get("integral", "")
        nodes, amp, node_sens, source_sens, source_ids = _analyze_sample(batch, cfg)
        if not nodes:
            no_id_records += len(batch)
            continue

        samples_seen[integral] = samples_seen.get(integral, 0) + 1
        I = integrals.setdefault(
            integral, {"regions": {}, "variables": {}, "cascade_chains": {}})

        prov_var_names: set[str] = set()
        for r in nodes.values():
            prov_var_names.update(_prov_vars(r))

        for rid, r in nodes.items():
            loc = _region_key(r)
            reg = I["regions"].setdefault(loc, _new_region())
            local_vars = _region_local_reads(r, source_ids, prov_var_names)
            _update_region(reg, r, amp[rid], node_sens[rid], cfg, local_vars)

        for sid, sens in source_sens.items():
            var = I["variables"].setdefault(
                sid, {"max_sensitivity": 0.0, "max_amp": 0.0,
                      "n_consumers": 0, "is_source_var": sid in prov_var_names})
            if sens > var["max_sensitivity"]:
                var["max_sensitivity"] = sens
            if sens > var["max_amp"]:
                var["max_amp"] = sens
            var["n_consumers"] += 1
            var["is_source_var"] = var["is_source_var"] or (sid in prov_var_names)

        I["cascade_chains"].update(_extract_cascade_chains(
            nodes, amp, node_sens, source_ids, prov_var_names,
            integral, scope, cfg))

    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": "stability_shard_report",
        "samples_seen": samples_seen,
        "no_id_records": no_id_records,
        "integrals": {name: _integral_to_json(name, data)
                      for name, data in integrals.items()},
    }
    if not legacy:
        report["nonfinite_records"] = nonfinite_records
    return report


def _integral_to_json(name: str, data: dict) -> dict:
    regions = {}
    for loc, reg in data["regions"].items():
        r = dict(reg)
        r["rel_err_hist"] = reg["rel_err_hist"].to_dict()
        r["prov_vars"] = sorted(reg["prov_vars"])
        r["region_local_vars"] = sorted(reg["region_local_vars"])
        r["integral"] = name          # v2: explicit parent-bucket tag
        regions[loc] = r
    return {"regions": regions, "variables": data["variables"],
            "cascade_chains": data.get("cascade_chains", {})}


# ---------------------------------------------------------------------------
# Merge (combine shard reports)
# ---------------------------------------------------------------------------

def merge_reports(reports: list[dict]) -> dict:
    """Combine shard reports into one merged report (associative, order-free).

    ``merge([reduce(A), reduce(B)]) == reduce(A ++ B)`` for every aggregate:
    maxes via ``max``, counts/hist via addition, value ranges via min/max, sets
    via union.
    """
    out_samples: dict[str, int] = {}
    out_integrals: dict[str, dict] = {}
    no_id = 0
    # additive v1-read field: present in the merge iff any input shard has it
    # (legacy-mode outputs must stay byte-identical to the historical reducer)
    nonfinite = 0
    have_nonfinite = False

    for rep in reports:
        no_id += rep.get("no_id_records", 0)
        if "nonfinite_records" in rep:
            have_nonfinite = True
            nonfinite += rep.get("nonfinite_records", 0)
        for name, cnt in rep.get("samples_seen", {}).items():
            out_samples[name] = out_samples.get(name, 0) + cnt
        for name, idata in rep.get("integrals", {}).items():
            dst = out_integrals.setdefault(
                name, {"regions": {}, "variables": {}, "cascade_chains": {}})
            for loc, reg in idata.get("regions", {}).items():
                mreg = dst["regions"].setdefault(loc, _new_region_json())
                _merge_region(mreg, reg)
                mreg["integral"] = name    # v2: authoritative parent-bucket tag
            for vid, var in idata.get("variables", {}).items():
                _merge_variable(dst["variables"].setdefault(vid, _new_variable_json()), var)
            # cascade chains are per-(sample, victim) — union by chain_id, never
            # merged even when they share lines (the consumer owns overlap).
            dst.setdefault("cascade_chains", {}).update(idata.get("cascade_chains", {}))

    merged = {
        "schema_version": SCHEMA_VERSION,
        "kind": "stability_merged_report",
        "samples_seen": out_samples,
        "no_id_records": no_id,
        "integrals": out_integrals,
    }
    if have_nonfinite:
        merged["nonfinite_records"] = nonfinite
    return merged


def _new_region_json() -> dict:
    return {"ops": {}, "n": 0, "max_cond": 0.0, "gate_a_count": 0,
            "max_rel_err": 0.0, "rel_err_hist": {"buckets": {}, "total": 0},
            "max_sensitivity": 0.0, "max_amp": 0.0,
            "abs_val_min": None, "abs_val_max": None, "prov_vars": [],
            "region_local_vars": [], "integral": ""}


def _new_variable_json() -> dict:
    return {"max_sensitivity": 0.0, "max_amp": 0.0, "n_consumers": 0,
            "is_source_var": False}


def _merge_region(dst: dict, src: dict) -> None:
    for op, c in src.get("ops", {}).items():
        dst["ops"][op] = dst["ops"].get(op, 0) + c
    dst["n"] += src.get("n", 0)
    dst["max_cond"] = max(dst["max_cond"], src.get("max_cond", 0.0))
    dst["gate_a_count"] += src.get("gate_a_count", 0)
    dst["max_rel_err"] = max(dst["max_rel_err"], src.get("max_rel_err", 0.0))
    h = LogHist.from_dict(dst["rel_err_hist"])
    h.merge(LogHist.from_dict(src.get("rel_err_hist", {})))
    dst["rel_err_hist"] = h.to_dict()
    dst["max_sensitivity"] = max(dst["max_sensitivity"], src.get("max_sensitivity", 0.0))
    dst["max_amp"] = max(dst["max_amp"], src.get("max_amp", 0.0))
    dst["abs_val_min"] = _min_opt(dst["abs_val_min"], src.get("abs_val_min"))
    dst["abs_val_max"] = _max_opt(dst["abs_val_max"], src.get("abs_val_max"))
    dst["prov_vars"] = sorted(set(dst["prov_vars"]) | set(src.get("prov_vars", [])))
    dst["region_local_vars"] = sorted(
        set(dst["region_local_vars"]) | set(src.get("region_local_vars", [])))
    # v2: carry the parent-bucket tag through the merge so consumers that only
    # call _merge_region (a partitioned merge) inherit it without a separate
    # stamp.  All src regions for a given loc share one bucket, so this is
    # order-independent; empty on legacy (v1) shards that carry no tag.
    if src.get("integral"):
        dst["integral"] = src["integral"]


def _merge_variable(dst: dict, src: dict) -> None:
    dst["max_sensitivity"] = max(dst["max_sensitivity"], src.get("max_sensitivity", 0.0))
    dst["max_amp"] = max(dst["max_amp"], src.get("max_amp", 0.0))
    dst["n_consumers"] += src.get("n_consumers", 0)
    dst["is_source_var"] = dst["is_source_var"] or src.get("is_source_var", False)


def _min_opt(a, b):
    vals = [x for x in (a, b) if x is not None]
    return min(vals) if vals else None


def _max_opt(a, b):
    vals = [x for x in (a, b) if x is not None]
    return max(vals) if vals else None


# ---------------------------------------------------------------------------
# Finalize (mechanistic classification -> consolidated report)
# ---------------------------------------------------------------------------
#
# The report is POLICY-NEUTRAL.  It carries measured quantities and a
# mechanistic signal class; it does NOT decide downcast/keep/upgrade or apply
# an acceptance margin — those are the consumer's job.

def _range_ok(reg: dict, cfg: ReducerConfig) -> bool:
    """Measured fact: do all recorded |val| at this region fit the target range?"""
    lo, hi = reg.get("abs_val_min"), reg.get("abs_val_max")
    if cfg.range_min_normal is not None and lo is not None and lo < cfg.range_min_normal:
        return False
    if cfg.range_max is not None and hi is not None and hi > cfg.range_max:
        return False
    return True


def chain_range_ok(chain: dict, classified_regions: dict,
                   cfg: ReducerConfig) -> bool:
    """A cascade chain is range-safe iff *every* contributor line is.

    Cascade-chain records carry no aggregated ``abs_val_min/max`` of their own
    (``_extract_cascade_chains`` unions source spans, not value ranges), so a
    range prune would have no signal on chains and fail open.  Derive it from
    the already-classified region records for the chain's contributor lines:
    the chain spans those lines, so if any one line's measured |val| leaves the
    target's normal range the whole chain is range-unsafe.  A contributor line
    with no region record defaults to safe (fail-open, matching
    ``_range_ok``'s missing-data behavior — an omission never silently
    *blocks* a downcast).
    """
    key = cfg.range_ok_key
    for span in chain.get("chain", []) or []:
        f = span.get("file")
        for ln in range(int(span.get("line_start", 0)),
                        int(span.get("line_end", 0)) + 1):
            reg = classified_regions.get(f"{f}:{ln}")
            if reg is not None and not reg.get(key, True):
                return False
    return True


def _signal_class(reg: dict, cfg: ReducerConfig) -> tuple[str, str]:
    """Mechanistic classification of the error phenomenon (no acceptance policy).

    Returns ``(class, note)`` where the note describes the *measurement*, never
    a remediation.  The residual "stable" class means only that no
    elevated-error mechanism was detected locally — NOT that the region is
    downcast-safe (that depends on the forward-cone amp and the caller's
    margin).
    """
    cond = reg.get("max_cond", 0.0)
    rel = reg.get("max_rel_err", 0.0)
    n = reg.get("n", 0)
    gate_a = reg.get("gate_a_count", 0)

    # Note strings must stay byte-identical to the legacy reducer when the
    # config equals the legacy defaults (differential-parity requirement), but
    # must not state thresholds the run did not use under an override.
    cap_str = ("2**53" if cfg.saturation_cap == SATURATION_CAP
               else repr(cfg.saturation_cap))
    thr_str = ("1e15" if cfg.local_cancel_cond == 1e15
               else repr(cfg.local_cancel_cond))

    if gate_a > 0 and cond == 0.0 and n == gate_a:
        # "atan2_saturation" is a historical class name kept for downstream
        # readers; the 1/u cap is emitted by log/sin/cos/atan2 AND add/sub
        # underflow-to-zero, all indistinguishable in pre-v1 journals.
        return "atan2_saturation", (f"atan2 saturation cap ({cap_str}) only; "
                                    "no genuine hotspot")
    if cond >= cfg.local_cancel_cond:
        return "local_cancellation", f"local cond {cond:.2e} exceeds {thr_str} (|a-b|->0)"
    if cond >= cfg.high_cond:
        return "log_near_root", f"elevated per-op cond {cond:.2e}"
    if rel >= cfg.cascade_rel_err and cond < cfg.cascade_cond_ceiling:
        return "cancellation_cascade", (f"rel_err {rel:.2e} with low per-op cond "
                                        f"{cond:.2e} (accumulated cancellation)")
    return "stable", "no elevated conditioning or accumulated-error signal"


def _classify_region(reg: dict, cfg: ReducerConfig,
                     integral: str | None = None) -> dict:
    cond = reg.get("max_cond", 0.0)
    sens = reg.get("max_sensitivity", 0.0)
    cls, note = _signal_class(reg, cfg)
    hist = LogHist.from_dict(reg.get("rel_err_hist", {}))
    return {
        # v2: explicit parent-integral tag.  finalize_report passes the bucket
        # name; a partitioned merge calls without it and we read the tag
        # _merge_region carried onto the region dict (empty on a legacy region).
        "integral": integral if integral is not None else reg.get("integral", ""),
        "signal_class": cls,                     # mechanistic; consumer branches on it
        "note": note,                            # describes the measurement, not a fix
        "non_localizable": cls == "cancellation_cascade",
        "max_cond": cond,
        "gate_a_count": reg.get("gate_a_count", 0),
        "max_rel_err": reg.get("max_rel_err", 0.0),
        "p50_rel_err": hist.quantile(0.50),
        "p99_rel_err": hist.quantile(0.99),
        "max_amp": reg.get("max_amp", 0.0),
        "max_sensitivity": sens,                 # cond * amp (forward cone)
        **_prediction_fields(sens, cfg),
        "abs_val_min": reg.get("abs_val_min"),
        "abs_val_max": reg.get("abs_val_max"),
        cfg.range_ok_key: _range_ok(reg, cfg),
        "n": reg.get("n", 0),
        "ops": reg.get("ops", {}),
        "prov_vars": reg.get("prov_vars", []),
        # Peer of prov_vars: source vars READ in-scope at this region's line(s)
        # (subset of prov_vars) — the region-scoped input for a regional
        # precision change.
        "region_local_vars": reg.get("region_local_vars", []),
    }


def _classify_variable(var: dict, cfg: ReducerConfig) -> dict:
    sens = var.get("max_sensitivity", 0.0)
    return {
        "max_amp": var.get("max_amp", 0.0),
        "max_sensitivity": sens,
        **_prediction_fields(sens, cfg),
        "n_consumers": var.get("n_consumers", 0),
        "is_source_var": var.get("is_source_var", False),
        # source values are not journaled by track(); the range guard is N/A.
        "value_range_checked": False,
    }


def finalize_report(merged: dict, cfg: ReducerConfig | None = None) -> dict:
    """Turn a merged report into the consolidated (policy-neutral) report.

    Ranking is by measured severity (``max_rel_err``), NOT by any remediation
    direction — the consumer applies its acceptance margin and picks the
    action per region/variable.
    """
    cfg = cfg or ReducerConfig()
    out_integrals: dict[str, dict] = {}

    for name, idata in merged.get("integrals", {}).items():
        regions = {loc: _classify_region(reg, cfg, name)
                   for loc, reg in idata.get("regions", {}).items()}
        variables = {vid: _classify_variable(var, cfg)
                     for vid, var in idata.get("variables", {}).items()
                     if var.get("is_source_var")}

        class_counts: dict[str, int] = {}
        for r in regions.values():
            class_counts[r["signal_class"]] = class_counts.get(r["signal_class"], 0) + 1

        # cascade chains: emitted as a list, deterministic (chain_id asc), never
        # merged.  Each is a localized replacement for a non_localizable cascade
        # region.
        chains = idata.get("cascade_chains", {})
        cascade_chains = [chains[cid] for cid in sorted(chains)]
        # Stamp the range flag on each chain from its contributor regions
        # (chains carry no abs_val range of their own), so a range prune does
        # not fail open on chain records.
        for ch in cascade_chains:
            ch[cfg.range_ok_key] = chain_range_ok(ch, regions, cfg)

        out_integrals[name] = {
            "samples": merged.get("samples_seen", {}).get(name, 0),
            "class_counts": class_counts,
            "top_regions_by_rel_err": [
                {"location": loc, **regions[loc]}
                # deterministic: severity desc, then location asc to break ties,
                # so the list does not depend on merge/insertion order.
                for loc in sorted(regions,
                                  key=lambda l: (-regions[l]["max_rel_err"], l))
            ][:10],
            "regions": regions,
            "variables": variables,
            "cascade_chains": cascade_chains,
        }

    final = {
        "schema_version": SCHEMA_VERSION,
        "kind": "stability_report",
        "samples_seen": merged.get("samples_seen", {}),
        "no_id_records": merged.get("no_id_records", 0),
        "integrals": out_integrals,
    }
    if "nonfinite_records" in merged:
        final["nonfinite_records"] = merged["nonfinite_records"]
    return final


def report_from_journals(paths: list, cfg: ReducerConfig | None = None, *,
                         legacy: bool = False) -> dict:
    """Convenience: reduce N shard journals, merge, finalize."""
    cfg = cfg or ReducerConfig()
    return finalize_report(
        merge_reports([reduce_journal(p, cfg, legacy=legacy) for p in paths]), cfg)
