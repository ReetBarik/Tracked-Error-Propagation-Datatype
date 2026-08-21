# tracked-tools

Pip-installable analysis tools for `Tracked<T>` JSONL journals.

```bash
pip install -e "tools[test]"        # from the repo root
```

## tracked-reduce

Journal → mergeable code-region stability report (the sharded-characterization
map/merge/finalize pipeline). See `tracked_tools/reduce/core.py` for the full
measurement model (per-sample DAG, forward-cone amplification, cascade-chain
localization, mergeable log-histograms).

```bash
tracked-reduce reduce shard0/journal.jsonl -o shard0.report.json
tracked-reduce merge shard*.report.json -o report.json
tracked-reduce report journal.jsonl -o report.json     # reduce+merge+finalize
```

The policy seam (`ReducerConfig`) injects the consumer's downcast target
formats and thresholds; the default predicts for IEEE single. Add formats with
`--predict name=unit_roundoff`.

Differential-parity guarantee: with the AMP-consumer config, the reducer
reproduces AMP's historical `stability_reducer.py` byte-for-byte on the frozen
v0.3 fixture journals (`tests/parity/`); CI enforces this against committed
goldens.

## Coming in this package

- `tracked-line-inject` — libclang per-statement `line=` scope injection.
- `tracked-boundary-patch` — compiler-error-driven int↔tracked boundary patches.
- `tracked_tools/interop/` — LLM interop-shim kit (ruleset, caching, plumbing).
