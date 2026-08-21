"""``tracked-view`` console entry point.

Distill a run (a directory of ``*.jsonl`` shards, or explicit journal paths)
into a self-contained static HTML view; optionally dump the view-model JSON.

    tracked-view viewer/fixtures/demo -o view.html
    tracked-view shard0.jsonl shard1.jsonl -o view.html --json model.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tracked_tools.reduce.core import ReducerConfig
from tracked_tools.view.distill import build_html, distill_run, model_json


def _resolve_journals(inputs: list[str]) -> list[Path]:
    paths: list[Path] = []
    for spec in inputs:
        p = Path(spec)
        if p.is_dir():
            found = sorted(p.glob("*.jsonl"))
            if not found:
                raise SystemExit(f"{p}: no *.jsonl journals in run directory")
            paths.extend(found)
        elif p.is_file():
            paths.append(p)
        else:
            raise SystemExit(f"{spec}: no such file or directory")
    return paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Distill Tracked journal runs into a static HTML viewer.")
    ap.add_argument("run", nargs="+",
                    help="run directory (*.jsonl globbed, sorted) or journal files")
    ap.add_argument("-o", "--out", required=True, help="output HTML path")
    ap.add_argument("--json", metavar="PATH",
                    help="also write the view-model JSON")
    ap.add_argument("--legacy", action="store_true",
                    help="read pre-v1 (headerless v0.3) journals; without this "
                         "flag a v1 header record is hard-required")
    ap.add_argument("--predict", action="append", metavar="NAME=U",
                    help="additional target format: emit predicted rel_err for "
                         "NAME using unit roundoff U (repeatable)")
    ap.add_argument("--saturation-cap", type=float, metavar="X",
                    help="override the 1/u saturation-cap value the numeric "
                         "gate tests against (set for non-double-T journals, "
                         "e.g. 2**24 for float)")
    ap.add_argument("--hotspot-cap", type=int, default=20, metavar="N",
                    help="max hotspot cards distilled (default: 20)")
    args = ap.parse_args(argv)

    cfg = ReducerConfig()
    for spec in args.predict or []:
        name, sep, value = spec.partition("=")
        if not sep or not name:
            raise SystemExit(f"--predict expects name=unit_roundoff, got: {spec!r}")
        cfg.predictions[name] = float(value)
    if args.saturation_cap is not None:
        cfg.saturation_cap = args.saturation_cap

    journals = _resolve_journals(args.run)
    model = distill_run(journals, cfg, legacy=args.legacy,
                        hotspot_cap=args.hotspot_cap)

    if args.json:
        Path(args.json).write_text(
            json.dumps(json.loads(model_json(model).replace("<\\/", "</")),
                       indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    Path(args.out).write_text(build_html(model), encoding="utf-8")
    n_hot = len(model["report"]["hotspots"])
    print(f"{args.out}: {model['totals']['units']} units, "
          f"{model['totals']['samples']} samples, "
          f"{n_hot}/{model['report']['hotspots_total']} hotspots")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
