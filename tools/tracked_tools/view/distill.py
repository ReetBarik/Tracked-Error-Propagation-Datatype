"""Journal run -> view model (the ``tracked-view`` distiller core).

The view model is the single JSON object the bundled HTML template renders —
a private contract between this module and ``template.html`` shipped in the
same ``tracked-tools`` version. ``viewer/README.md`` (repo) documents it and
the design decisions; the load-bearing properties are:

* **Policy comes from the reducer, unchanged.** Classification, thresholds,
  the numeric gate-a test, amplification/sensitivity, and cascade-chain
  extraction are all imported from :mod:`tracked_tools.reduce.core`. This
  module runs its own streaming loop only because it needs per-record data
  the reducer's aggregation discards (cap/exact_tie/non-finite tallies,
  region-to-region dataflow edges, header metadata, one embedded drill-down
  sample) — nothing threshold-like is re-derived.

* **Shard invariance.** Everything outside the ``provenance`` block is
  identical whether a fully-scoped run is distilled from one monolithic
  journal or from ``--sample-offset`` shard files: aggregations are
  order-free (max/sum/union), collections carry canonical sort orders,
  drill-down operands are batch-local indices, leaf ids with shard-varying
  ``#<counter>`` suffixes are normalized to callsite + batch-local ordinal,
  and cascade chains are structurally deduped (the reducer's per-sample
  ``chain_id`` hashes a raw id and is dropped).

* **The shard-disjointness precondition is validated**, not assumed: a
  non-empty sample scope seen in two inputs (or non-contiguously within
  one) hard-fails; unscoped records in a multi-journal read get a ledger
  warning (they are excluded from the invariance guarantee).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from tracked_tools.reduce import core as rc

VIEW_MODEL_SCHEMA = 1

# Fraction of double's ~15.95 significant decimal digits, used for the
# hotspot cards' digits-lost figure: digits ~ clamp(16 + log10(rel_err), 0, 16).
DIGITS_TRACK = 16.0

_UNATTRIBUTED = "(unattributed)"


# ---------------------------------------------------------------------------
# Region / function keying (display-side companions of rc._region_key)
# ---------------------------------------------------------------------------

def _region_info(rec: dict) -> tuple[str, str, str, int | None]:
    """``(key, kind, file, line)`` for a record's code region.

    ``key`` equals ``rc._region_key(rec)`` exactly (classification parity);
    ``kind`` says *structurally* which source produced it — "line" for a
    ``line=`` scope value (``file:line``), "at" for a located call
    (``file:function:line``), "" for neither — so display parsing never
    guesses the shape from colon counts.
    """
    line_scope = rc._parse_scope(rc._scope_str(rec.get("id", ""))).get("line")
    if line_scope:
        file, _, ln = line_scope.rpartition(":")
        return line_scope, "line", (file or line_scope), (int(ln) if ln.isdigit() else None)
    at = rec.get("at", "") or ""
    if at:
        parts = at.rsplit(":", 2)
        if len(parts) == 3 and parts[2].isdigit():
            return at, "at", parts[0], int(parts[2])
        return at, "at", at, None
    return "", "", "", None


def _at_function(rec: dict) -> str | None:
    """``file:function`` of a record's located call, or None when at is empty."""
    at = rec.get("at", "") or ""
    if not at:
        return None
    parts = at.rsplit(":", 2)
    if len(parts) == 3:
        return f"{parts[0]}:{parts[1]}"
    return at


def _operands(rec: dict) -> list[str]:
    """Direct-operand ids. For ``opaque`` records, ``in[0]`` is the external
    fn_name (metadata, SCHEMA.md), not an operand — skip it."""
    ins = rec.get("in", []) or []
    if rec.get("op") == "opaque" and ins:
        return ins[1:]
    return ins


# ---------------------------------------------------------------------------
# Per-unit accumulator
# ---------------------------------------------------------------------------

class _Unit:
    def __init__(self) -> None:
        self.samples = 0
        self.records = 0
        self.regions: dict[str, dict] = {}          # rc._new_region() dicts
        self.region_extra: dict[str, dict] = {}     # cap/tie/nonfinite/kind/...
        self.edges: dict[tuple[str, str], int] = {}
        self.variables: dict[str, dict] = {}
        self.chain_groups: dict[tuple, dict] = {}
        # drill-down candidate: (max_rel_err, neg-less scope key, batch, sents)
        self.best: tuple[float, str, list, dict] | None = None

    def region(self, key: str, kind: str, file: str, line: int | None) -> tuple[dict, dict]:
        reg = self.regions.get(key)
        if reg is None:
            reg = self.regions[key] = rc._new_region()
            self.region_extra[key] = {
                "kind": kind, "file": file, "line": line,
                "cap_counts": {}, "exact_tie_count": 0, "nonfinite_count": 0,
                "at_fn_counts": {},
            }
        return reg, self.region_extra[key]


def _better_candidate(cur: tuple | None, rel: float, scope: str) -> bool:
    """Argmax on batch max rel_err; ties broken by smallest scope key —
    deterministic across shard splits (README: shard invariance)."""
    if cur is None:
        return True
    if rel != cur[0]:
        return rel > cur[0]
    return scope < cur[1]


# ---------------------------------------------------------------------------
# The distiller
# ---------------------------------------------------------------------------

def distill_run(paths, cfg: rc.ReducerConfig | None = None, *,
                legacy: bool = False, hotspot_cap: int = 20) -> dict:
    """Distill one run (one or more shard journals) into the view model."""
    cfg = cfg or rc.ReducerConfig()
    paths = [Path(p) for p in paths]
    if not paths:
        raise ValueError("no journals given")

    units: dict[str, _Unit] = {}
    headers: list[dict] = []
    totals_records = 0
    no_id_records = 0
    nonfinite_records = 0
    seen_scopes: dict[str, int] = {}       # non-empty sample key -> file index
    unscoped_multifile = False

    for file_idx, path in enumerate(paths):
        stream = rc.read_journal(path, legacy=legacy, on_header=headers.append)
        file_scopes: set[str] = set()
        prev_key: str | None = None
        for scope, batch in rc._iter_samples(stream):
            if scope:
                owner = seen_scopes.get(scope)
                if owner is not None and owner != file_idx:
                    raise ValueError(
                        f"{path}: sample scope {scope!r} already appeared in "
                        f"{paths[owner]} — shards must have disjoint sample "
                        "scopes (docs/SCHEMA.md concatenation rule)")
                if scope in file_scopes and scope != prev_key:
                    raise ValueError(
                        f"{path}: sample scope {scope!r} reappears "
                        "non-contiguously — the chunkable-driver contract "
                        "requires per-stream sample contiguity")
                seen_scopes[scope] = file_idx
                file_scopes.add(scope)
            elif len(paths) > 1:
                unscoped_multifile = True
            prev_key = scope

            nonfinite_records += _distill_batch(units, scope, batch, cfg)
            totals_records += len(batch)
            no_id_records += _no_id_len(batch)

    return _assemble(units, cfg, paths, headers, legacy=legacy,
                     totals_records=totals_records,
                     no_id_records=no_id_records,
                     nonfinite_records=nonfinite_records,
                     unscoped_multifile=unscoped_multifile,
                     hotspot_cap=hotspot_cap)


def _no_id_len(batch: list[dict]) -> int:
    return 0 if any(r.get("id") is not None for r in batch) else len(batch)


def _distill_batch(units: dict[str, _Unit], scope: str, batch: list[dict],
                   cfg: rc.ReducerConfig) -> int:
    """Process one per-sample batch; returns its non-finite record count."""
    sentinels: dict[str, dict] = {}
    nonfinite_ids: set[str] = set()
    nonfinite = 0
    for r in batch:
        sent = r.pop("_sentinels", None)
        if r.pop("_nonfinite", False):
            nonfinite += 1
            if r.get("id") is not None:
                nonfinite_ids.add(r["id"])
        if sent and r.get("id") is not None:
            sentinels[r["id"]] = sent

    nodes, amp, node_sens, source_sens, source_ids = rc._analyze_sample(batch, cfg)
    if not nodes:
        return nonfinite                     # id-less batch: counted by caller

    unit_name = rc._parse_scope(scope).get("integral", "")
    U = units.setdefault(unit_name, _Unit())
    U.samples += 1
    U.records += len(batch)

    prov_var_names: set[str] = set()
    prov_const_names: set[str] = set()
    for r in nodes.values():
        prov_var_names.update(rc._prov_vars(r))
        prov_const_names.update(r.get("prov_consts") or [])

    for rid, r in nodes.items():
        key, kind, file, line = _region_info(r)
        reg, extra = U.region(key, kind, file, line)
        local_vars = rc._region_local_reads(r, source_ids, prov_var_names)
        rc._update_region(reg, r, amp[rid], node_sens[rid], cfg, local_vars)
        cap = r.get("cap")
        if cap:
            extra["cap_counts"][cap] = extra["cap_counts"].get(cap, 0) + 1
        if r.get("exact_tie"):
            extra["exact_tie_count"] += 1
        if rid in nonfinite_ids:
            extra["nonfinite_count"] += 1
        fn = _at_function(r)
        if fn:
            extra["at_fn_counts"][fn] = extra["at_fn_counts"].get(fn, 0) + 1
        # dataflow edges: operand produced in another region of this sample
        for o in _operands(r):
            src_rec = nodes.get(o)
            if src_rec is None:
                continue
            src_key = rc._region_key(src_rec)
            if src_key != key:
                e = (src_key, key)
                U.edges[e] = U.edges.get(e, 0) + 1

    # named inputs only (README: literal and opaque-fn leaves are per-sample
    # ids, not actionable inputs)
    for sid, sens in source_sens.items():
        is_var = sid in prov_var_names
        if not is_var and sid not in prov_const_names:
            continue
        var = U.variables.setdefault(
            sid, {"is_source_var": is_var, "max_sensitivity": 0.0,
                  "max_amp": 0.0, "n_samples": 0})
        var["is_source_var"] = var["is_source_var"] or is_var
        if sens > var["max_sensitivity"]:
            var["max_sensitivity"] = sens
        if sens > var["max_amp"]:
            var["max_amp"] = sens
        var["n_samples"] += 1

    # cascade chains -> structural groups (README: dedup by span set + op set)
    chains = rc._extract_cascade_chains(nodes, amp, node_sens, source_ids,
                                        prov_var_names, unit_name, scope, cfg)
    for ch in chains.values():
        spans = tuple(sorted((s["file"], s["line_start"], s["line_end"])
                             for s in ch["chain"]))
        gkey = (spans, tuple(sorted(ch["ops"])))
        grp = U.chain_groups.get(gkey)
        if grp is None:
            grp = U.chain_groups[gkey] = {
                "spans": [{"file": f, "line_start": a, "line_end": b}
                          for f, a, b in spans],
                "ops": {}, "count": 0, "max_rel_err": 0.0, "max_cond": 0.0,
                "max_sensitivity": 0.0, "region_local_vars": set(),
            }
        grp["count"] += 1
        for op, n in ch["ops"].items():
            grp["ops"][op] = max(grp["ops"].get(op, 0), n)
        grp["max_rel_err"] = max(grp["max_rel_err"], ch["max_rel_err"])
        grp["max_cond"] = max(grp["max_cond"], ch["max_cond"])
        grp["max_sensitivity"] = max(grp["max_sensitivity"], ch["max_sensitivity"])
        grp["region_local_vars"].update(ch["region_local_vars"])

    # drill-down candidate
    batch_rel = 0.0
    for r in nodes.values():
        batch_rel = max(batch_rel, rc._rel_err(r))
    if _better_candidate(U.best, batch_rel, scope):
        U.best = (batch_rel, scope, batch, sentinels)

    return nonfinite


# ---------------------------------------------------------------------------
# Drill-down encoding (batch-local indices; normalized leaf keys)
# ---------------------------------------------------------------------------

def _leaf_key(raw: str, ordinals: dict[str, str], counters: dict[str, int]) -> str:
    """Shard-invariant leaf key: named sources/constants pass through; ids
    with a shard-varying ``#<counter>`` (literals; out-of-batch generated ids)
    normalize to ``<callsite>~<k>`` with k the batch-local first-reference
    ordinal per callsite (emission order is shard-invariant per sample)."""
    got = ordinals.get(raw)
    if got is not None:
        return got
    if "#" not in raw:
        ordinals[raw] = raw
        return raw
    base = raw.split("#", 1)[0]
    k = counters.get(base, 0)
    counters[base] = k + 1
    key = f"{base}~{k}"
    ordinals[raw] = key
    return key


def _build_drilldown(unit_name: str, best: tuple | None) -> dict | None:
    if best is None:
        return None
    batch_rel, scope, batch, sentinels = best
    id_index: dict[str, int] = {}
    ordered = [r for r in batch if r.get("id") is not None]
    for i, r in enumerate(ordered):
        id_index[r["id"]] = i

    leaf_ordinals: dict[str, str] = {}
    leaf_counters: dict[str, int] = {}
    nodes = []
    sources: set[str] = set()
    for i, r in enumerate(ordered):
        sent = sentinels.get(r["id"], {})

        def field(k: str):
            if k in sent:
                return sent[k]               # "nan"/"inf"/"-inf", verbatim
            return r.get(k)

        ins: list = []
        for o in _operands(r):
            j = id_index.get(o)
            if j is not None and j < i:
                ins.append(j)
            else:
                key = _leaf_key(o, leaf_ordinals, leaf_counters)
                ins.append(key)
                sources.add(key)
        node = {
            "op": r.get("op", ""),
            "region": rc._region_key(r),
            "in": ins,
            "val": field("val"), "cond": field("cond"),
            "rel_err": field("rel_err"),
            "cap": r.get("cap") or None,
            "exact_tie": bool(r.get("exact_tie", False)),
        }
        if r.get("op") == "opaque" and (r.get("in") or []):
            node["label"] = r["in"][0]
        nodes.append(node)

    sample = rc._parse_scope(scope).get("sample")
    rerun = None
    if unit_name and sample is not None and sample.isdigit():
        rerun = (f"--unit {unit_name} --sample-offset {sample} "
                 f"--sample-count 1")
    return {
        "sample_scope": scope,
        "sample": sample,
        "max_rel_err": batch_rel,
        "rerun": rerun,
        "nodes": nodes,
        "sources": sorted(sources),
    }


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _predictions(sens: float, cfg: rc.ReducerConfig) -> dict[str, float]:
    return {name: u * sens for name, u in sorted(cfg.predictions.items())}


def _digits_lost(rel_err: float) -> float:
    if rel_err <= 0.0:
        return 0.0
    return max(0.0, min(DIGITS_TRACK, DIGITS_TRACK + math.log10(rel_err)))


def _assemble(units: dict[str, _Unit], cfg: rc.ReducerConfig, paths, headers,
              *, legacy: bool, totals_records: int, no_id_records: int,
              nonfinite_records: int, unscoped_multifile: bool,
              hotspot_cap: int) -> dict:
    unit_list = []
    ledger: list[dict] = []
    hotspot_pool: list[dict] = []

    lib_versions = sorted({h.get("library_version", "") for h in headers
                           if h.get("library_version")})

    # -- run-level ledger rows (canonical order: fixed sequence) ---------------
    if legacy:
        ledger.append({"unit": "*", "subject": "run", "verdict": "legacy_mode",
                       "detail": "read as pre-v1 (headerless) journals: cap, "
                                 "exact-tie, and non-finite tallies are not "
                                 "measurable (shown as n/a, never 0)"})
    if len(lib_versions) > 1:
        ledger.append({"unit": "*", "subject": "run",
                       "verdict": "library_version_skew",
                       "detail": "shards were emitted by different library "
                                 "builds: " + ", ".join(lib_versions)})
    if no_id_records:
        ledger.append({"unit": "*", "subject": "run", "verdict": "no_id_records",
                       "detail": f"{no_id_records} records without ids "
                                 "(pre-v0.3) carry no DAG and were skipped"})
    if not legacy and nonfinite_records:
        ledger.append({"unit": "*", "subject": "run",
                       "verdict": "nonfinite_records",
                       "detail": f"{nonfinite_records} records carried a "
                                 "non-finite val/cond/rel_err sentinel; "
                                 "aggregates clamp them to the alarm "
                                 "direction (docs/SCHEMA.md)"})
    if unscoped_multifile:
        ledger.append({"unit": "*", "subject": "run",
                       "verdict": "unscoped_shards",
                       "detail": "unscoped records appeared in a multi-journal "
                                 "read; unscoped units are excluded from the "
                                 "shard-invariance guarantee"})

    for name in sorted(units):
        U = units[name]
        regions_out = []
        class_counts: dict[str, int] = {}
        functions: dict[str, dict] = {}
        classified: dict[str, dict] = {}     # for chain_range_ok

        for key in sorted(U.regions):
            reg = U.regions[key]
            extra = U.region_extra[key]
            regj = dict(reg)
            regj["rel_err_hist"] = reg["rel_err_hist"].to_dict()
            regj["prov_vars"] = sorted(reg["prov_vars"])
            regj["region_local_vars"] = sorted(reg["region_local_vars"])
            cls = rc._classify_region(regj, cfg, name)
            classified[key] = cls

            fn_key = _function_of(key, extra)
            fn = functions.setdefault(fn_key, {"key": fn_key, "regions": []})
            fn["regions"].append(key)

            class_counts[cls["signal_class"]] = \
                class_counts.get(cls["signal_class"], 0) + 1

            hist = regj["rel_err_hist"]
            region_out = {
                "key": key, "kind": extra["kind"], "file": extra["file"],
                "line": extra["line"], "function": fn_key,
                "signal_class": cls["signal_class"], "note": cls["note"],
                "n": regj["n"], "ops": regj["ops"],
                "max_cond": cls["max_cond"],
                "gate_a_count": cls["gate_a_count"],
                "cap_counts": None if legacy else extra["cap_counts"],
                "exact_tie_count": None if legacy else extra["exact_tie_count"],
                "nonfinite_count": None if legacy else extra["nonfinite_count"],
                "max_rel_err": cls["max_rel_err"],
                "p50_rel_err": cls["p50_rel_err"],
                "p99_rel_err": cls["p99_rel_err"],
                "rel_err_hist": hist,
                "zero_rel_err": regj["n"] - hist["total"],
                "max_amp": cls["max_amp"],
                "max_sensitivity": cls["max_sensitivity"],
                "predictions": _predictions(cls["max_sensitivity"], cfg),
                "abs_val_min": cls["abs_val_min"],
                "abs_val_max": cls["abs_val_max"],
                "range_ok": cls[cfg.range_ok_key],
                "region_local_vars": cls["region_local_vars"],
            }
            regions_out.append(region_out)

            caps = extra["cap_counts"]
            ledger.append({
                "unit": name, "subject": key or "(unattributed ops)",
                "kind": "region", "verdict": cls["signal_class"],
                "detail": cls["note"],
                "range": "ok" if cls[cfg.range_ok_key] else _range_reason(cls, cfg),
                "caps": None if legacy else caps,
                "exact_ties": None if legacy else extra["exact_tie_count"],
            })
            if not legacy and sum(caps.values()) != cls["gate_a_count"]:
                ledger.append({
                    "unit": name, "subject": key or "(unattributed ops)",
                    "kind": "region", "verdict": "cap_gate_skew",
                    "detail": (f"branch-authoritative cap count "
                               f"{sum(caps.values())} != numeric 1/u gate "
                               f"count {cls['gate_a_count']} — if this journal "
                               "came from a non-double T, override "
                               "--saturation-cap"),
                })
            if not legacy and extra["nonfinite_count"]:
                ledger.append({
                    "unit": name, "subject": key or "(unattributed ops)",
                    "kind": "region", "verdict": "nonfinite_values",
                    "detail": (f"{extra['nonfinite_count']} records with "
                               "non-finite val/cond/rel_err at this region"),
                })

        # chain groups (range flag from classified contributor regions)
        chains_out = []
        chain_lines: set[tuple[str, int]] = set()
        for gkey in sorted(U.chain_groups):
            grp = U.chain_groups[gkey]
            pseudo = {"chain": grp["spans"]}
            range_ok = rc.chain_range_ok(pseudo, classified, cfg)
            chains_out.append({
                "spans": grp["spans"], "ops": grp["ops"], "count": grp["count"],
                "max_rel_err": grp["max_rel_err"], "max_cond": grp["max_cond"],
                "max_sensitivity": grp["max_sensitivity"],
                "predictions": _predictions(grp["max_sensitivity"], cfg),
                "range_ok": range_ok,
                "region_local_vars": sorted(grp["region_local_vars"]),
            })
            for s in grp["spans"]:
                for ln in range(s["line_start"], s["line_end"] + 1):
                    chain_lines.add((s["file"], ln))
            spans_str = "; ".join(f"{s['file']}:{s['line_start']}"
                                  + (f"-{s['line_end']}"
                                     if s["line_end"] != s["line_start"] else "")
                                  for s in grp["spans"])
            ledger.append({
                "unit": name, "subject": spans_str, "kind": "chain",
                "verdict": "cancellation_cascade",
                "detail": (f"cascade chain over {len(grp['spans'])} line(s) in "
                           f"{grp['count']} sample(s); max rel_err "
                           f"{grp['max_rel_err']:.2e}"),
                "range": "ok" if range_ok else "violates target range",
            })

        # functions: fill file/name, order regions canonically
        functions_out = []
        for fn_key in sorted(functions):
            f = functions[fn_key]
            if fn_key == _UNATTRIBUTED:
                file, fname = "", _UNATTRIBUTED
            else:
                file, _, fname = fn_key.rpartition(":")
            functions_out.append({"key": fn_key, "file": file, "name": fname,
                                  "regions": sorted(f["regions"])})

        variables_out = {
            vid: {"is_source_var": v["is_source_var"],
                  "max_sensitivity": v["max_sensitivity"],
                  "max_amp": v["max_amp"], "n_samples": v["n_samples"],
                  "predictions": _predictions(v["max_sensitivity"], cfg)}
            for vid, v in sorted(U.variables.items())
        }

        unit_max_rel = max(
            [r["max_rel_err"] for r in regions_out]
            + [c["max_rel_err"] for c in chains_out] + [0.0])

        unit_list.append({
            "name": name,
            "samples": U.samples,
            "records": U.records,
            "class_counts": class_counts,
            "max_rel_err": unit_max_rel,
            "functions": functions_out,
            "regions": regions_out,
            "edges": [{"src": s, "dst": d, "n": n}
                      for (s, d), n in sorted(U.edges.items())],
            "chains": chains_out,
            "variables": variables_out,
            "drilldown": _build_drilldown(name, U.best),
        })

        # hotspot pool: regions + chain groups.  A cascade-class region whose
        # line a chain group covers is represented by the chain (the reducer
        # documents chains as the localized replacement); it stays in
        # units[].regions.  The covered-line match compares span tuples built
        # from region keys via the same _parse_region_span on both sides, so
        # it holds within one attribution style; a source line that appears
        # both as a line=-keyed region and an at-keyed region (mixed
        # attribution across records) is treated as two distinct lines.
        for r in regions_out:
            if r["signal_class"] == "cancellation_cascade":
                span = rc._parse_region_span(r["key"])
                if span is not None and (span["file"], span["line_start"]) in chain_lines:
                    continue
            hotspot_pool.append({
                "unit": name, "kind": "region", "location": r["key"],
                "signal_class": r["signal_class"], "note": r["note"],
                "metrics": _hotspot_metrics(r),
            })
        for c in chains_out:
            loc = "; ".join(f"{s['file']}:{s['line_start']}" for s in c["spans"])
            hotspot_pool.append({
                "unit": name, "kind": "chain", "location": loc,
                "signal_class": "cancellation_cascade",
                "note": (f"cascade chain over {len(c['spans'])} line(s), "
                         f"{c['count']} sample(s)"),
                "metrics": _hotspot_metrics(c),
            })

    hotspot_pool.sort(key=lambda h: (-h["metrics"]["max_rel_err"], h["kind"],
                                     h["unit"], h["location"]))
    hotspots = [{"rank": i + 1, **h} for i, h in enumerate(hotspot_pool[:hotspot_cap])]

    ledger.sort(key=lambda row: (row["unit"] != "*", row["unit"],
                                 row.get("kind", ""), row["subject"],
                                 row["verdict"]))

    cfg_out = {
        "local_cancel_cond": cfg.local_cancel_cond,
        "high_cond": cfg.high_cond,
        "cascade_rel_err": cfg.cascade_rel_err,
        "cascade_cond_ceiling": cfg.cascade_cond_ceiling,
        "cascade_cancel_ratio": cfg.cascade_cancel_ratio,
        "saturation_cap": cfg.saturation_cap,
        "predictions": dict(sorted(cfg.predictions.items())),
        "range_name": cfg.range_name,
        "range_min_normal": cfg.range_min_normal,
        "range_max": cfg.range_max,
    }

    return {
        "kind": "tracked_view_model",
        "view_model_schema": VIEW_MODEL_SCHEMA,
        "provenance": {"generator": "tracked-tools",
                       "journals": [p.name for p in paths]},
        "legacy": legacy,
        "library_versions": lib_versions,
        "config": cfg_out,
        "totals": {
            "records": totals_records,
            "samples": sum(u.samples for u in units.values()),
            "units": len(units),
            "no_id_records": no_id_records,
            "nonfinite_records": None if legacy else nonfinite_records,
        },
        "units": unit_list,
        "report": {
            "hotspots_total": len(hotspot_pool),
            "hotspots": hotspots,
            "ledger": ledger,
        },
    }


def _function_of(key: str, extra: dict) -> str:
    """A region's function key (README: at-keyed -> its own function;
    line=-keyed -> modal at-function, lexicographic tie-break)."""
    if extra["kind"] == "at":
        parts = key.rsplit(":", 2)
        if len(parts) == 3 and parts[2].isdigit():
            return f"{parts[0]}:{parts[1]}"
        return key
    counts = extra["at_fn_counts"]
    if counts:
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return _UNATTRIBUTED


def _range_reason(cls: dict, cfg: rc.ReducerConfig) -> str:
    lo, hi = cls.get("abs_val_min"), cls.get("abs_val_max")
    reasons = []
    if cfg.range_min_normal is not None and lo is not None and lo < cfg.range_min_normal:
        reasons.append(f"|val| down to {lo:.3e} underflows {cfg.range_name}")
    if cfg.range_max is not None and hi is not None and hi > cfg.range_max:
        reasons.append(f"|val| up to {hi:.3e} overflows {cfg.range_name}")
    return "; ".join(reasons) or "violates target range"


def _hotspot_metrics(entry: dict) -> dict:
    m = {
        "max_rel_err": entry["max_rel_err"],
        "digits_lost": _digits_lost(entry["max_rel_err"]),
        "max_cond": entry["max_cond"],
        "max_sensitivity": entry["max_sensitivity"],
        "predictions": entry["predictions"],
        "range_ok": entry["range_ok"],
    }
    if "p99_rel_err" in entry:
        m["p99_rel_err"] = entry["p99_rel_err"]
    if "n" in entry:
        m["n"] = entry["n"]
    if "count" in entry:
        m["samples"] = entry["count"]
    return m


# ---------------------------------------------------------------------------
# HTML build
# ---------------------------------------------------------------------------

def _sanitize(x):
    """Clamp non-finite floats to the alarm direction (±DBL_MAX).

    Aggregation arithmetic over alarm-clamped inputs (e.g. sensitivity =
    cond * amp with a clamped cond) can overflow to inf; the view model is
    strict JSON (no Infinity/NaN literals), so clamp on the way out — same
    direction the clamp already leaned.  Drill-down sentinel *strings* pass
    through untouched.
    """
    if isinstance(x, float) and not math.isfinite(x):
        if math.isnan(x):
            return rc.DBL_MAX_CLAMP
        return rc.DBL_MAX_CLAMP if x > 0 else -rc.DBL_MAX_CLAMP
    if isinstance(x, dict):
        return {k: _sanitize(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_sanitize(v) for v in x]
    return x


def model_json(model: dict) -> str:
    """Canonical serialization (README: sorted keys, so model equality is
    byte-equality). ``</`` is escaped so the JSON is inert inside <script>."""
    return json.dumps(_sanitize(model), sort_keys=True, separators=(",", ":"),
                      allow_nan=False).replace("</", "<\\/")


_MARKER = "__TRACKED_VIEW_MODEL_JSON__"


def build_html(model: dict, template_path=None) -> str:
    """Inject the view model into the bundled template -> self-contained HTML."""
    if template_path is None:
        template_path = Path(__file__).with_name("template.html")
    template = Path(template_path).read_text(encoding="utf-8")
    if _MARKER not in template:
        raise ValueError(f"{template_path}: no {_MARKER} injection marker")
    return template.replace(_MARKER, model_json(model), 1)
