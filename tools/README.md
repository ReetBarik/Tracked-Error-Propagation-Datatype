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

## tracked-line-inject

libclang-driven per-statement `line=<basename>:<N>` scope injection: emits a
`git apply`-able patch wrapping every value-producing statement in a target
header tree so operator arithmetic (which carries no `SourceLocation`) gets
line-level attribution. Declarations wrap with `push_scope`/`pop_scope`;
everything else with an RAII block. Parameterized on extra include dirs,
defines, and the RAII scope variable name; composes with a boundary (C8)
patch; `.hash`-sidecar regeneration cache. Requires the `libclang` extra
(`pip install -e "tools[inject]"`) and, for real trees, `g++` on PATH.

## tracked-boundary-patch

Compiler-error-driven int↔tracked boundary annotation: feed it the gcc stderr
of a shimmed build and it maps the three recognized crossing patterns
(tracked→int assignment, int→tracked ref bind, tracked-vs-int comparison) to
exact source edits, synthesized as a deterministic unified diff with an
exact-once edit discipline. Unrecognized int↔tracked diagnostics hard-fail
(`C8_UNCLASSIFIED_ERROR`) for human review. Parameterized on the instrumented
scalar spelling (`--type tracked::Tracked`). gcc diagnostics only (v1).

## tracked_tools/interop/

The interop-shim kit: the classification ruleset (`ruleset.txt`, Rules 1–9 +
C1–C7 — byte-pinned, consumers' cached `SOURCE_HASH`es depend on it), the
SOURCE_HASH staleness cache (`cache.py`), and LLM generation plumbing
(`llm.py`; anthropic-SDK streaming as the default transport, pluggable via
any callable). Library-side shim conventions: `docs/INTEROP.md`.
