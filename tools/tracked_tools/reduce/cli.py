"""``tracked-reduce`` console entry point.

Subcommands:
  reduce   one journal -> mergeable shard report
  report   reduce + merge + finalize N journals -> consolidated report
  merge    merge + finalize N shard reports -> consolidated report

The default policy predicts for IEEE single ("float").  Add further target
formats with ``--predict name=unit_roundoff`` (repeatable), e.g. a float-float
emulation format: ``--predict ff=1.4210854715202004e-14`` (2**-46).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tracked_tools.reduce.core import (
    ReducerConfig,
    finalize_report,
    merge_reports,
    reduce_journal,
    report_from_journals,
)


def _write_json(obj: dict, path) -> None:
    Path(path).write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")


def _config_from_args(args: argparse.Namespace) -> ReducerConfig:
    cfg = ReducerConfig()
    for spec in args.predict or []:
        name, sep, value = spec.partition("=")
        if not sep or not name:
            raise SystemExit(f"--predict expects name=unit_roundoff, got: {spec!r}")
        cfg.predictions[name] = float(value)
    return cfg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tracked journal stability reducer.")
    ap.add_argument("--predict", action="append", metavar="NAME=U",
                    help="additional target format: emit predicted_rel_err_if_NAME "
                         "using unit roundoff U (repeatable)")
    ap.add_argument("--legacy", action="store_true",
                    help="read pre-v1 (headerless v0.3) journals; without this "
                         "flag a v1 header record is hard-required")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_red = sub.add_parser("reduce", help="reduce one journal -> shard report")
    p_red.add_argument("journal")
    p_red.add_argument("-o", "--out", required=True)

    p_rep = sub.add_parser("report", help="reduce+merge+finalize N journals -> report")
    p_rep.add_argument("journals", nargs="+")
    p_rep.add_argument("-o", "--out", required=True)

    p_mrg = sub.add_parser("merge", help="merge+finalize N shard reports -> report")
    p_mrg.add_argument("shards", nargs="+")
    p_mrg.add_argument("-o", "--out", required=True)

    args = ap.parse_args(argv)
    cfg = _config_from_args(args)

    if args.cmd == "reduce":
        _write_json(reduce_journal(args.journal, cfg, legacy=args.legacy), args.out)
    elif args.cmd == "report":
        _write_json(report_from_journals(args.journals, cfg, legacy=args.legacy),
                    args.out)
    elif args.cmd == "merge":
        shards = [json.loads(Path(s).read_text(encoding="utf-8")) for s in args.shards]
        _write_json(finalize_report(merge_reports(shards), cfg), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
