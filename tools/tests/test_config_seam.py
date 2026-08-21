"""The ReducerConfig policy seam actually steers behavior, and legacy input
shapes reduce identically in the port and the frozen AMP reference.

The parity harness (test_parity_v03.py) runs everything at AMP-default config
values, where a port that silently hardcoded the legacy constants would be
indistinguishable.  These tests flip each seam knob to a NON-default value and
assert the output moves — plus differential coverage for the legacy/degraded
input shapes the fixtures deliberately never produce (id-less v0.2 records,
flat ``prov``, shard JSON without hist totals, integral-less legacy shards).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tracked_tools.reduce import ReducerConfig, core

REPO = Path(__file__).resolve().parents[2]
REFERENCE = REPO / "tests" / "parity" / "reference" / "amp_stability_reducer_frozen.py"


@pytest.fixture(scope="module")
def frozen():
    spec = importlib.util.spec_from_file_location("amp_frozen_seam", REFERENCE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["amp_frozen_seam"] = mod
    spec.loader.exec_module(mod)
    return mod


def write_journal(tmp_path, records, name="j.jsonl"):
    p = tmp_path / name
    p.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return p


def rec(op="mul", ident="m#1@integral=k/sample=0", ins=("a", "b"), val=1.0,
        cond=1.0, rel_err=1e-16, at="f.h:fn:3", **extra):
    r = {"op": op, "at": at, "id": ident, "in": list(ins), "val": val,
         "cond": cond, "rel_err": rel_err, "prov_vars": ["a"], "prov_consts": []}
    r.update(extra)
    return r


# ---------------------------------------------------------------------------
# Seam knobs move the output
# ---------------------------------------------------------------------------

def test_saturation_cap_is_injectable(tmp_path):
    """gate-a counting keys off cfg.saturation_cap, not a hardcoded 2**53."""
    j = write_journal(tmp_path, [rec(op="atan2", cond=2.0 ** 24)])
    default = core.reduce_journal(j, ReducerConfig(), legacy=True)
    reg = next(iter(default["integrals"]["k"]["regions"].values()))
    assert reg["gate_a_count"] == 0 and reg["max_cond"] == 2.0 ** 24

    lowered = core.reduce_journal(j, ReducerConfig(saturation_cap=2.0 ** 24),
                                  legacy=True)
    reg = next(iter(lowered["integrals"]["k"]["regions"].values()))
    assert reg["gate_a_count"] == 1 and reg["max_cond"] == 0.0

    # ...and the classification note names the configured cap, not 2**53
    cfg = ReducerConfig(saturation_cap=2.0 ** 24)
    final = core.finalize_report(core.merge_reports([lowered]), cfg)
    region = next(iter(final["integrals"]["k"]["regions"].values()))
    assert region["signal_class"] == "atan2_saturation"
    assert "2**53" not in region["note"]


def test_local_cancel_threshold_note_is_truthful(tmp_path):
    j = write_journal(tmp_path, [rec(op="sub", cond=1e13, rel_err=1e-4)])
    cfg = ReducerConfig(local_cancel_cond=1e12)
    final = core.finalize_report(
        core.merge_reports([core.reduce_journal(j, cfg, legacy=True)]), cfg)
    region = next(iter(final["integrals"]["k"]["regions"].values()))
    assert region["signal_class"] == "local_cancellation"
    assert "1e15" not in region["note"]

    # default config keeps the legacy byte-exact spelling
    j2 = write_journal(tmp_path, [rec(op="sub", cond=1e16, rel_err=1e-4)], "j2.jsonl")
    final = core.finalize_report(
        core.merge_reports([core.reduce_journal(j2, ReducerConfig(), legacy=True)]),
        ReducerConfig())
    region = next(iter(final["integrals"]["k"]["regions"].values()))
    assert "exceeds 1e15" in region["note"]


def test_range_guard_bounds_and_name_are_injectable(tmp_path):
    j = write_journal(tmp_path, [rec(val=1e-45)])   # below float min normal
    base_cfg = ReducerConfig()
    final = core.finalize_report(
        core.merge_reports([core.reduce_journal(j, base_cfg, legacy=True)]), base_cfg)
    region = next(iter(final["integrals"]["k"]["regions"].values()))
    assert region["value_range_ok_for_float"] is False

    # widen the bounds (an fp64 guard): now in range
    cfg = ReducerConfig(range_name="double", range_min_normal=2.3e-308,
                        range_max=1.7e308)
    final = core.finalize_report(
        core.merge_reports([core.reduce_journal(j, cfg, legacy=True)]), cfg)
    region = next(iter(final["integrals"]["k"]["regions"].values()))
    assert "value_range_ok_for_float" not in region
    assert region["value_range_ok_for_double"] is True


def test_mechanistic_thresholds_are_injectable(tmp_path):
    # rel_err 1e-8 with cond 1: stable by default, cascade when the victim
    # threshold is lowered
    j = write_journal(tmp_path, [rec(op="add", cond=1.0, rel_err=1e-8)])
    final = core.finalize_report(
        core.merge_reports([core.reduce_journal(j, ReducerConfig(), legacy=True)]),
        ReducerConfig())
    region = next(iter(final["integrals"]["k"]["regions"].values()))
    assert region["signal_class"] == "stable"

    cfg = ReducerConfig(cascade_rel_err=1e-9)
    final = core.finalize_report(
        core.merge_reports([core.reduce_journal(j, cfg, legacy=True)]), cfg)
    region = next(iter(final["integrals"]["k"]["regions"].values()))
    assert region["signal_class"] == "cancellation_cascade"


def test_prediction_formats_follow_config(tmp_path):
    j = write_journal(tmp_path, [rec()])
    cfg = ReducerConfig(predictions={"qf": 2.0 ** -100})
    final = core.finalize_report(
        core.merge_reports([core.reduce_journal(j, cfg, legacy=True)]), cfg)
    region = next(iter(final["integrals"]["k"]["regions"].values()))
    assert "predicted_rel_err_if_qf" in region
    assert "predicted_rel_err_if_float" not in region


# ---------------------------------------------------------------------------
# Legacy / degraded input shapes: differential vs the frozen reference
# ---------------------------------------------------------------------------

def diff_reduce(frozen, path):
    """Run both implementations (AMP-equivalent config on the port)."""
    cfg = ReducerConfig(predictions={"float": core.U_FLOAT, "ff": 2.0 ** -46})
    return core.reduce_journal(path, cfg, legacy=True), frozen.reduce_journal(path)


def test_no_id_records_differential(tmp_path, frozen):
    """v0.2 journals (no ids) degrade identically: counted, bucket skipped."""
    v02 = [{"op": "sub", "at": "f.h:fn:3", "in": ["a", "b"], "val": 1e-12,
            "cond": 2e12, "rel_err": 2e-4, "prov": ["a", "b"]} for _ in range(5)]
    j = write_journal(tmp_path, v02)
    new, ref = diff_reduce(frozen, j)
    assert new == ref
    assert new["no_id_records"] == 5 and new["integrals"] == {}

    # mixed batch: an id-less record shares a batch with id-bearing records
    # only when both key to the same sample scope (here: unscoped, key "");
    # inside such a batch it is dropped WITHOUT being counted (documented
    # legacy behavior, pinned differentially)
    mixed = [rec(ident="m#1"), {"op": "sub", "at": "f.h:fn:4", "in": ["a", "b"],
                                "val": 1.0, "cond": 1.0, "rel_err": 1e-16,
                                "prov": ["a"]}]
    j2 = write_journal(tmp_path, mixed, "mixed.jsonl")
    new, ref = diff_reduce(frozen, j2)
    assert new == ref
    assert new["no_id_records"] == 0
    assert set(new["integrals"]) == {""}     # unscoped bucket


def test_flat_prov_fallback_differential(tmp_path, frozen):
    """Pre-v0.3 flat ``prov`` feeds prov_vars in both implementations."""
    r = rec()
    del r["prov_vars"], r["prov_consts"]
    r["prov"] = ["legacy_var"]
    j = write_journal(tmp_path, [r])
    new, ref = diff_reduce(frozen, j)
    assert new == ref
    reg = next(iter(new["integrals"]["k"]["regions"].values()))
    assert reg["prov_vars"] == ["legacy_var"]


def test_legacy_shard_merge_differential(frozen):
    """Hand-written v1-era shard JSON (no hist total, no integral tags) merges
    identically."""
    legacy_shard = {
        "schema_version": 1,
        "kind": "stability_shard_report",
        "samples_seen": {"k": 1},
        "no_id_records": 0,
        "integrals": {"k": {
            "regions": {"f.h:3": {
                "ops": {"mul": 1}, "n": 1, "max_cond": 5.0, "gate_a_count": 0,
                "max_rel_err": 1e-10,
                "rel_err_hist": {"buckets": {"-10": 1}},   # no "total" key
                "max_sensitivity": 5.0, "max_amp": 1.0,
                "abs_val_min": 1.0, "abs_val_max": 2.0,
                "prov_vars": ["a"], "region_local_vars": [],
                # no "integral" tag (pre-v2 shard)
            }},
            "variables": {"a": {"max_sensitivity": 5.0, "max_amp": 5.0,
                                "n_consumers": 1, "is_source_var": True}},
            "cascade_chains": {},
        }},
    }
    cfg = ReducerConfig(predictions={"float": core.U_FLOAT, "ff": 2.0 ** -46})
    new = core.finalize_report(core.merge_reports(
        [json.loads(json.dumps(legacy_shard))]), cfg)
    ref = frozen.finalize_report(frozen.merge_reports(
        [json.loads(json.dumps(legacy_shard))]))
    assert new == ref
    reg = new["integrals"]["k"]["regions"]["f.h:3"]
    assert reg["p50_rel_err"] == 1e-10 and reg["integral"] == "k"
