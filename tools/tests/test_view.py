"""tracked-view distiller tests.

Three pillars (viewer/README.md):
  1. shard invariance — distill(monolithic fixture) == distill(sharded
     fixture) byte-for-byte outside the provenance block;
  2. the feature-coverage matrix (viewer/fixtures/README.md) — every render
     path the template has is present in the distilled demo model;
  3. contract validation — disjointness/contiguity violations hard-fail,
     legacy degrades to null (not zero), opaque/literal leaf handling.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from tracked_tools.reduce import core as rc
from tracked_tools.view import distill

REPO = Path(__file__).resolve().parents[2]
DEMO = REPO / "viewer" / "fixtures" / "demo" / "journal.jsonl"
SHARDS = sorted((REPO / "viewer" / "fixtures" / "demo_sharded").glob("*.jsonl"))

HEADER = {"schema": 1, "library_version": "1.0.0",
          "keys": ["op", "at", "id", "in", "val", "cond", "rel_err",
                   "prov_vars", "prov_consts"]}


def rec(op="mul", ident="mul@f.h:3#1@integral=k/sample=0", at="f.h:fn:3",
        in_=("a", "b"), val=1.0, cond=1.0, rel_err=1e-16, **extra):
    r = {"op": op, "at": at, "id": ident, "in": list(in_), "val": val,
         "cond": cond, "rel_err": rel_err, "prov_vars": ["a"],
         "prov_consts": []}
    r.update(extra)
    return r


def write(tmp_path, lines, name="j.jsonl"):
    p = tmp_path / name
    p.write_text("".join(json.dumps(l) + "\n" for l in lines),
                 encoding="utf-8")
    return p


@pytest.fixture(scope="module")
def demo_model():
    return distill.distill_run([DEMO])


@pytest.fixture(scope="module")
def sharded_model():
    return distill.distill_run(SHARDS)


# ---------------------------------------------------------------------------
# 1. shard invariance
# ---------------------------------------------------------------------------

def test_sharded_model_identical_outside_provenance(demo_model, sharded_model):
    a = {k: v for k, v in demo_model.items() if k != "provenance"}
    b = {k: v for k, v in sharded_model.items() if k != "provenance"}
    # byte-equality of the canonical serialization is the contract
    assert (distill.model_json(a) == distill.model_json(b))


def test_provenance_reflects_inputs(demo_model, sharded_model):
    assert demo_model["provenance"]["journals"] == ["journal.jsonl"]
    assert sharded_model["provenance"]["journals"] == [p.name for p in SHARDS]
    assert demo_model["provenance"]["generator"] == "tracked-tools"


def test_distill_deterministic(demo_model):
    again = distill.distill_run([DEMO])
    assert distill.model_json(again) == distill.model_json(demo_model)


# ---------------------------------------------------------------------------
# 2. feature coverage (viewer/fixtures/README.md matrix)
# ---------------------------------------------------------------------------

def _unit(model, name):
    for u in model["units"]:
        if u["name"] == name:
            return u
    raise AssertionError(f"unit {name!r} missing")


def test_all_signal_classes_present(demo_model):
    classes = set()
    for u in demo_model["units"]:
        classes.update(u["class_counts"])
    assert classes == {"stable", "local_cancellation", "log_near_root",
                       "cancellation_cascade", "atan2_saturation"}


def test_cap_causes_present(demo_model):
    caps = set()
    for u in demo_model["units"]:
        for r in u["regions"]:
            caps.update(r["cap_counts"] or {})
    assert {"log", "atan2", "sin", "cos", "nan"} <= caps


def test_exact_ties_in_kahan(demo_model):
    kahan = _unit(demo_model, "kahan")
    assert sum(r["exact_tie_count"] for r in kahan["regions"]) > 0


def test_range_violation_and_nonfinite_in_range_overflow(demo_model):
    u = _unit(demo_model, "range_overflow")
    assert any(not r["range_ok"] for r in u["regions"])
    assert sum(r["nonfinite_count"] for r in u["regions"]) > 0
    assert demo_model["totals"]["nonfinite_records"] > 0


def test_chain_groups_single_and_multi_span(demo_model):
    spans = {len(c["spans"])
             for u in demo_model["units"] for c in u["chains"]}
    assert 1 in spans and 2 in spans
    sd = _unit(demo_model, "second_difference")
    assert sd["chains"], "second_difference must carry a chain group"
    assert sd["chains"][0]["count"] == sd["samples"]


def test_region_kinds_line_at_and_functions(demo_model):
    kinds = {r["kind"] for u in demo_model["units"] for r in u["regions"]}
    assert {"line", "at"} <= kinds
    nv = _unit(demo_model, "naive_variance")
    fn_keys = {f["key"] for f in nv["functions"]}
    assert any(k.endswith(":mean_of") for k in fn_keys)
    assert any(k.endswith(":naive_variance") for k in fn_keys)
    # the line=-keyed variance sub attributes to its modal at-function
    line_regions = [r for r in nv["regions"] if r["kind"] == "line"]
    assert line_regions and all(
        r["function"].endswith(":naive_variance") for r in line_regions)


def test_dataflow_edges_cross_function(demo_model):
    canc = _unit(demo_model, "cancellation")
    assert canc["edges"], "cancellation unit must have dataflow edges"
    keys = {r["key"]: r for r in canc["regions"]}
    for e in canc["edges"]:
        assert e["src"] in keys and e["dst"] in keys and e["src"] != e["dst"]
    fn = {keys[e["src"]]["function"] for e in canc["edges"]}
    assert any(k.endswith(":guarded_sum") for k in fn)


def test_opaque_node_label_not_operand(demo_model):
    u = _unit(demo_model, "complex_logdiv")
    dd = u["drilldown"]
    opaque = [n for n in dd["nodes"] if n["op"] == "opaque"]
    assert opaque
    for n in opaque:
        assert n["label"] == "ext::blas_scale"
        assert "ext::blas_scale" not in [i for i in n["in"] if isinstance(i, str)]
    assert "ext::blas_scale" not in dd["sources"]
    assert "ext::blas_scale" not in u["variables"]


def test_drilldown_encoding(demo_model):
    for u in demo_model["units"]:
        dd = u["drilldown"]
        assert dd is not None
        for i, n in enumerate(dd["nodes"]):
            for o in n["in"]:
                if isinstance(o, int):
                    assert 0 <= o < i, "operand index must reference an earlier node"
                else:
                    assert o in dd["sources"]
        # rerun recipe well-formed for these named, numbered fixtures
        assert dd["rerun"] == (f"--unit {u['name']} --sample-offset "
                               f"{dd['sample']} --sample-count 1")


def test_drilldown_sentinels_not_clamped(demo_model):
    dd = _unit(demo_model, "range_overflow")["drilldown"]
    vals = [n["val"] for n in dd["nodes"]]
    assert "inf" in vals and "nan" in vals
    assert rc.DBL_MAX_CLAMP not in vals


def test_drilldown_literal_leaves_normalized(demo_model):
    ks = _unit(demo_model, "kahan")["drilldown"]
    lits = [s for s in ks["sources"] if s.startswith("_lit@")]
    assert lits, "kahan feeds literal terms"
    assert all("#" not in s and "~" in s for s in lits), \
        "literal leaves must be callsite~ordinal, never raw #counter ids"
    assert len(set(lits)) == len(lits)


def test_variables_only_named_inputs(demo_model):
    for u in demo_model["units"]:
        for vid, v in u["variables"].items():
            assert not vid.startswith("_lit@")
            assert "#" not in vid
            assert v["n_samples"] <= u["samples"]
    canc = _unit(demo_model, "cancellation")
    assert canc["variables"]["a"]["is_source_var"]


def test_hotspots_ranked_and_capped(demo_model):
    hs = demo_model["report"]["hotspots"]
    assert hs and len(hs) <= 20
    assert demo_model["report"]["hotspots_total"] >= len(hs)
    rels = [h["metrics"]["max_rel_err"] for h in hs]
    assert rels == sorted(rels, reverse=True)
    assert [h["rank"] for h in hs] == list(range(1, len(hs) + 1))
    for h in hs:
        assert 0.0 <= h["metrics"]["digits_lost"] <= 16.0


def test_no_hotspot_duplicates_chain_covered_cascade_region(demo_model):
    """Invariant over the whole model: a cascade-class region hotspot never
    shares a line with a chain-group span in its unit (chains supersede)."""
    covered = {u["name"]: {(s["file"], s["line_start"])
                           for c in u["chains"] for s in c["spans"]}
               for u in demo_model["units"]}
    for h in demo_model["report"]["hotspots"]:
        if h["kind"] != "region" or h["signal_class"] != "cancellation_cascade":
            continue
        span = rc._parse_region_span(h["location"])
        if span is not None:
            assert (span["file"], span["line_start"]) not in covered[h["unit"]]


def test_chain_covered_cascade_region_superseded(tmp_path):
    """Direct coverage of the supersede skip (no fixture region hits it):
    a cancellation_cascade region on a chain-span line stays in
    units[].regions but is excluded from report.hotspots."""
    mk = lambda op, line, in_, val, cond, rel: rec(
        op=op, ident=f"{op}@f.h:{line}#1@integral=k/sample=0",
        at=f"f.h:fn:{line}", in_=in_, val=val, cond=cond, rel_err=rel)
    r1 = mk("sub", 1, ("a", "b"), 1.0, 1e11, 2e-5)      # elevated, contributor
    r1b = mk("mul", 9, ("a", "c"), -0.95, 1.0, 1e-16)   # benign internal
    r2 = mk("sub", 2, ("sub@f.h:1#1@integral=k/sample=0",
                       "mul@f.h:9#1@integral=k/sample=0"),
            0.05, 39.0, 4e-4)                           # near-cancel: cascade class
    r3 = mk("add", 3, ("sub@f.h:2#1@integral=k/sample=0", "a"),
            1.05, 1.0, 4e-4)                            # benign sink: chain victim
    j = write(tmp_path, [HEADER, r1, r1b, r2, r3])
    model = distill.distill_run([j])
    u = model["units"][0]

    reg2 = next(r for r in u["regions"] if r["key"] == "f.h:fn:2")
    assert reg2["signal_class"] == "cancellation_cascade"
    span_lines = {(s["file"], s["line_start"])
                  for c in u["chains"] for s in c["spans"]}
    assert ("f.h:fn", 2) in span_lines, "r2's line must be a chain span"

    locs = {(h["kind"], h["location"]) for h in model["report"]["hotspots"]}
    assert ("region", "f.h:fn:2") not in locs, \
        "chain-covered cascade region must be superseded in hotspots"
    assert any(k == "chain" for k, _ in locs)
    # ...but never dropped from the unit's region list
    assert any(r["key"] == "f.h:fn:2" for r in u["regions"])


def test_ledger_covers_regions_chains_and_run_rows(demo_model):
    ledger = demo_model["report"]["ledger"]
    verdicts = {row["verdict"] for row in ledger}
    assert "nonfinite_records" in verdicts          # run row
    kinds = {row.get("kind") for row in ledger}
    assert "region" in kinds and "chain" in kinds
    # every region has a ledger row
    n_regions = sum(len(u["regions"]) for u in demo_model["units"])
    n_region_rows = sum(1 for row in ledger
                        if row.get("kind") == "region"
                        and row["verdict"] not in ("cap_gate_skew",
                                                   "nonfinite_values"))
    assert n_region_rows == n_regions


def test_totals_consistent(demo_model):
    t = demo_model["totals"]
    assert t["units"] == len(demo_model["units"]) == 9
    assert t["samples"] == sum(u["samples"] for u in demo_model["units"])
    assert t["records"] == sum(u["records"] for u in demo_model["units"])
    assert t["no_id_records"] == 0
    assert demo_model["library_versions"] == ["1.0.0"]


def test_model_json_strict_and_escaped(demo_model):
    s = distill.model_json(demo_model)
    assert "</" not in s.replace("<\\/", "")
    parsed = json.loads(s.replace("<\\/", "</"))
    assert parsed["view_model_schema"] == distill.VIEW_MODEL_SCHEMA

    def walk(x):
        if isinstance(x, float):
            assert math.isfinite(x)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(parsed)


# ---------------------------------------------------------------------------
# 3. contract validation & degradation
# ---------------------------------------------------------------------------

def test_duplicate_scope_across_shards_hard_fails(tmp_path):
    a = write(tmp_path, [HEADER, rec()], "a.jsonl")
    b = write(tmp_path, [HEADER, rec()], "b.jsonl")
    with pytest.raises(ValueError, match="disjoint"):
        distill.distill_run([a, b])


def test_noncontiguous_scope_within_file_hard_fails(tmp_path):
    r1 = rec(ident="mul@f.h:3#1@integral=k/sample=0")
    r2 = rec(ident="mul@f.h:3#2@integral=k/sample=1")
    r3 = rec(ident="mul@f.h:3#3@integral=k/sample=0")
    j = write(tmp_path, [HEADER, r1, r2, r3])
    with pytest.raises(ValueError, match="contiguity"):
        distill.distill_run([j])


def test_unscoped_multifile_gets_ledger_warning(tmp_path):
    a = write(tmp_path, [HEADER, rec(ident="mul@f.h:3#1")], "a.jsonl")
    b = write(tmp_path, [HEADER, rec(ident="mul@f.h:4#1", at="f.h:fn:4")],
              "b.jsonl")
    model = distill.distill_run([a, b])
    assert any(row["verdict"] == "unscoped_shards"
               for row in model["report"]["ledger"])


def test_legacy_mode_nulls_not_zeros(tmp_path):
    j = write(tmp_path, [rec()])
    model = distill.distill_run([j], legacy=True)
    assert model["legacy"] is True
    assert model["library_versions"] == []
    assert model["totals"]["nonfinite_records"] is None
    reg = model["units"][0]["regions"][0]
    assert reg["cap_counts"] is None
    assert reg["exact_tie_count"] is None
    assert reg["nonfinite_count"] is None
    assert any(row["verdict"] == "legacy_mode"
               for row in model["report"]["ledger"])


def test_legacy_mode_rejects_v1(tmp_path):
    j = write(tmp_path, [HEADER, rec()])
    with pytest.raises(ValueError, match="not pre-v1"):
        distill.distill_run([j], legacy=True)


def test_v1_mode_rejects_headerless(tmp_path):
    j = write(tmp_path, [rec()])
    with pytest.raises(ValueError, match="legacy"):
        distill.distill_run([j])


def test_no_id_records_get_run_ledger_row(tmp_path):
    r = {"op": "mul", "at": "", "val": 1.0, "cond": 1.0, "rel_err": 1e-16,
         "prov": ["a"]}
    j = write(tmp_path, [r])
    model = distill.distill_run([j], legacy=True)
    assert model["totals"]["no_id_records"] == 1
    assert model["units"] == []
    assert any(row["verdict"] == "no_id_records"
               for row in model["report"]["ledger"])


def test_empty_journal_empty_model(tmp_path):
    j = tmp_path / "empty.jsonl"
    j.write_text("", encoding="utf-8")
    model = distill.distill_run([j])
    assert model["units"] == []
    assert model["totals"]["records"] == 0


def test_library_version_skew_surfaced(tmp_path):
    h2 = dict(HEADER, library_version="1.1.0")
    a = write(tmp_path, [HEADER, rec()], "a.jsonl")
    b = write(tmp_path, [h2, rec(ident="mul@f.h:3#1@integral=k/sample=1")],
              "b.jsonl")
    model = distill.distill_run([a, b])
    assert model["library_versions"] == ["1.0.0", "1.1.0"]
    assert any(row["verdict"] == "library_version_skew"
               for row in model["report"]["ledger"])


def test_cap_gate_skew_row_for_non_double_cap(tmp_path):
    # a float-T style journal: capped at 2^24, invisible to the numeric gate
    r = rec(cond=float(2 ** 24), cap="log", rel_err=1.0)
    j = write(tmp_path, [HEADER, r])
    model = distill.distill_run([j])
    assert any(row["verdict"] == "cap_gate_skew"
               for row in model["report"]["ledger"])
    reg = model["units"][0]["regions"][0]
    assert reg["cap_counts"] == {"log": 1}
    assert reg["gate_a_count"] == 0


# ---------------------------------------------------------------------------
# 4. HTML build, self-containment, committed-demo freshness
# ---------------------------------------------------------------------------

DEMO_HTML = REPO / "viewer" / "demo" / "view.html"


def test_template_self_contained():
    tpl = (Path(distill.__file__).parent / "template.html").read_text(encoding="utf-8")
    assert "<script src" not in tpl and "<link" not in tpl
    # the SVG namespace URI is a constant, not a network reference
    stripped = tpl.replace("http://www.w3.org/2000/svg", "")
    assert "http://" not in stripped and "https://" not in stripped
    assert "fetch(" not in tpl and "XMLHttpRequest" not in tpl
    assert "innerHTML" not in tpl, "untrusted strings must go through textContent"
    # exactly one injection marker, inside the JSON script block
    assert tpl.count("__TRACKED_VIEW_MODEL_JSON__") == 1


def test_committed_demo_is_fresh(demo_model):
    """viewer/demo/view.html == a fresh build over the committed fixtures.

    Regenerate with:
        tracked-view viewer/fixtures/demo -o viewer/demo/view.html
    (Deterministic: canonical JSON + no version strings in the output.)
    """
    fresh = distill.build_html(demo_model)
    committed = DEMO_HTML.read_text(encoding="utf-8")
    assert committed == fresh, (
        "committed demo is stale — regenerate: "
        "tracked-view viewer/fixtures/demo -o viewer/demo/view.html")


def test_cli_end_to_end(tmp_path):
    from tracked_tools.view.cli import main
    out = tmp_path / "v.html"
    js = tmp_path / "m.json"
    rc_ = main([str(REPO / "viewer" / "fixtures" / "demo"),
                "-o", str(out), "--json", str(js)])
    assert rc_ == 0
    model = json.loads(js.read_text(encoding="utf-8"))
    assert model["kind"] == "tracked_view_model"
    html = out.read_text(encoding="utf-8")
    assert "__TRACKED_VIEW_MODEL_JSON__" not in html
    assert '"tracked_view_model"' in html


def test_build_html_injects_model(demo_model, tmp_path):
    template = tmp_path / "t.html"
    template.write_text("<html><script id=\"vm\" type=\"application/json\">"
                        "__TRACKED_VIEW_MODEL_JSON__</script></html>",
                        encoding="utf-8")
    html = distill.build_html(demo_model, template_path=template)
    assert "__TRACKED_VIEW_MODEL_JSON__" not in html
    assert '"tracked_view_model"' in html
