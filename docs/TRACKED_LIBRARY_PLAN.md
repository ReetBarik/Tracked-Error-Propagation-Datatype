# Tracked Library Plan (2026-08-20)

> **Forward-looking handoff document** for executing the Tracked librarization from a fresh
> session. Self-contained: all ground-truth facts below were verified against source at the
> stated file:line during the 2026-08-20 audit session. The work happens in the Tracked repo
> itself (restructure-in-place); AMP consumes the result via subtree.
> Companion: [AMP `docs/IMPROVEMENT_PLAN.md`](https://github.com/ReetBarik/Agentic-Mixed-Precision-Demo/blob/main/docs/IMPROVEMENT_PLAN.md) (AMP-side objectives; §5.D and §6
> are the Tracked-side roadmap this plan implements).

## Ratified decisions (Reet, 2026-08-20)

1. **Name**: repo renamed to `Tracked-Error-Propagation-Datatype` (dropping `-Demo`).
   **Namespace `tracked::`, CMake target `tracked::tracked`, and `<tracked/...>` include
   paths are KEPT** — AMP's migration is a subtree-remote swap with zero code churn.
2. **License: MIT** (user delegated "whichever is user-friendly for open source"; MIT is the
   most frictionless for adopters. Tradeoff noted: Apache-2.0 adds an explicit patent grant —
   flag to Reet at PR time in case he wants to flip; it's a one-file change at this stage).
3. **Restructure-in-place**: work in a clone of the existing repo
   (`https://github.com/ReetBarik/Tracked-Error-Propagation-Datatype-Demo`), push, then
   rename the repo on GitHub (redirects preserve the old URL, so AMP's documented subtree
   pull command keeps working; update AMP's README anyway).
4. Monorepo layout: `include/tracked/` (C++), `tools/` (pip-installable Python package),
   `viewer/`, `tests/`, `docs/`.
5. C++17 kept; libclang an optional extra (injector only); C8 stays gcc-diagnostics-only in
   v1 (documented).
6. Viewer: zero-npm, dependency-free single-file static HTML.
7. CI: GitHub Actions — C++ build+ctest (ubuntu + macos), Python tests, schema differential
   tests against fixture journals.
8. **Acceptance bar (point 14)**: (a) C++ tests green; (b) differential test — new reducer
   output == AMP's `agents/shared/stability_reducer.py` on fixture journals (byte-compatible
   shard reports, parsed-value float compare); (c) AMP switched to the new repo via subtree
   with its full offline suite still passing (baseline: 981 passed / 8 skipped / 1
   pre-existing macOS `nm` failure); (d) one fixture characterization runs end-to-end locally
   through the new tools.
9. **⛔ HARD STOP after point 14.** Point 15 (cluster-side qcdloop re-characterization) runs
   on the cluster, not this Mac — **check in with Reet before starting it.**

## Execution sessions & sequencing override

**Sequencing override (critical, applies regardless of session split):** build the
`tracked-reduce` differential-parity harness against the **current v0.3 schema** and get it
green **before** landing the Phase-2 schema break. Never change the format and the
implementation in the same step — the harness is the regression net for the break; parity
failures after a simultaneous change are undiagnosable.

**Parity-fixture warning (verified by execution):** the committed AMP fixture journals
(`runs/*/journal.jsonl`) are pre-v0.3 — no `id` field, flat `prov` (`in` exists but holds
source names, not record ids) — and the current reducer reduces them to an **empty report**
(every record lands in `no_id_records`), so a parity harness built on them is **vacuously
green**. Generate fresh v0.3-schema journals for the harness (a small driver over example
kernels, built with the pre-break library, committed to this repo as parity fixtures) and
make the harness **assert the reduced reports are non-empty**.

**Session A — library core + switchover** (one session, this order):
P0 → P1 → P5 reducer port + parity harness (green on v0.3) → P2 schema break → P3 → P4 →
P5 remaining tools (injector, C8, interop kit) → P7 AMP switchover → acceptance point 14 →
**STOP** (point 15 is cluster-side; check in with Reet first).
Commit **and push at every phase boundary** so an interrupted session resumes from the last
green phase instead of restarting. Two verification passes worth an adversarial review:
(a) the schema-v1 design *before* it lands (the journal format becomes a public contract at
that moment); (b) the reducer generic-core vs AMP-policy split (the easiest place to
silently smuggle a behavior change past the parity harness).

**Session B — viewer (P6), fresh session/context:** regenerate fixture journals under v1,
then the distiller and the two views. Zero coupling to Session A beyond the v1 schema —
do **not** bolt it onto the end of Session A; it is design-quality work that deserves fresh
context (apply the dataviz guidance when writing chart/layout code).

## Source of truth

**This repo.** The AMP consumer vendors it at `AMP/third_party/tracked/` via git subtree.
The one divergence (AMP-side `push_scope`/`pop_scope`, AMP commit `e7e959f`) was synced
upstream on 2026-08-20 (commit preceding this doc) and the trees were verified identical.
AMP-side machinery to migrate is listed in Phase 5 with exact source paths (AMP =
`Agentic-Mixed-Precision-Demo`, sibling directory / same GitHub owner).

---

## Phase 0 — Repo surgery

- ~~Clone upstream; verify content matches `AMP/third_party/tracked/`~~ **DONE 2026-08-20**
  (push_scope/pop_scope synced upstream; trees identical).
- Add `LICENSE` (MIT), `NOTICE` if needed (the README "License/provenance" section currently
  discusses only formula provenance — keep the citations, add the software license).
- Restructure to the monorepo layout (headers already at `include/tracked/`; add `tools/`,
  `viewer/`).
- Rewrite `README.md` consumer-first (install, find_package, quick start, schema doc link,
  tools, viewer). Current README is good; extend rather than replace.
- GitHub: enable CI. The repo rename is a **manual admin action for Reet** at the very end
  (autonomous sessions must not attempt it — `gh` is not installed on this machine, and
  nothing blocks on the rename: the old URL keeps working via GitHub redirect).

## Phase 1 — Packaging & CI

Current state (verified): `CMakeLists.txt` has `add_library(tracked INTERFACE)` +
`tracked::tracked` alias + `cxx_std_17` + `$<INSTALL_INTERFACE:include>` genex — but **zero
`install()` rules** (the genex is dead code), no `project(VERSION)`, no version macro (only
`TRACKED_HERE` is defined across all headers), no tags, no CI, and tests build
unconditionally via Catch2 `FetchContent` (a consumer's `add_subdirectory` drags in Catch2;
needs network at configure).

Work:
- `project(tracked VERSION 1.0.0)`; generated `include/tracked/version.hpp`
  (`TRACKED_VERSION_{MAJOR,MINOR,PATCH}`, `TRACKED_JOURNAL_SCHEMA_VERSION`).
- `install(TARGETS tracked EXPORT ...)` + `install(DIRECTORY include/)` +
  `tracked-config.cmake` + version file → `find_package(tracked CONFIG)` works.
- `option(TRACKED_BUILD_TESTS "..." ${PROJECT_IS_TOP_LEVEL})` guarding the Catch2 fetch and
  test targets.
- GitHub Actions: matrix (ubuntu, macos) × (build+ctest); a Python job (tools tests +
  differential tests); tag-driven release job later.

## Phase 2 — Journal schema v1 (ONE coordinated break)

Current schema (verified): `flush()` writes exactly 9 keys —
`op,at,id,in,val,cond,rel_err,prov_vars,prov_consts` (`journal.hpp:143-157`) — **no version
field**; NaN→`null`, ±Inf→clamped ±1.7976931348623157e+308 (`journal.hpp:86-92`). Consumers
duck-type versions; the proven cost is AMP's `log_parser.py:50` silently reading dead keys.
Prior break precedent: v0.3 was a "schema-breaking hard cutover" with prose-only migration.

The v1 break (all at once):
1. **Header record** as line 1 of every journal:
   `{"schema": 1, "library_version": "...", "keys": [...]}`. Readers hard-require it or
   explicitly enter legacy mode.
2. **`cap` field** on capped records: the 1/u sentinel (2^53) is emitted by six distinct
   causes — log (`ops.hpp:56`), sin (`:92-94`), cos (`:114-116`), atan2 (`:136-138`), add
   underflow (`tracked.hpp:337`), sub underflow (`:364`) — currently indistinguishable
   downstream (all classify as atan2 saturation). Emit `"cap":"log|sin|cos|atan2|add_uflow|
   sub_uflow"` only on capped records. Also an `exact_tie` marker for the cond=1
   exact-cancellation special cases (`tracked.hpp:326-330, 356-359`).
3. **Non-lossy NaN/±Inf encoding**: string sentinels `"nan"/"inf"/"-inf"` (JSON-legal,
   unambiguous). Downstream readers must map explicitly — no more NaN→null→0.0
   ("maximally stable") path.
4. **Reserved scope grammar, documented and validated**: scopes are `key=value`, joined by
   `/` (`tracked.hpp:97-125`); therefore values MUST NOT contain `/` or `=` (today this rule
   exists only in AMP's `line_injector.py:28-33`); `line=` is reserved for statement regions;
   `debug_assert`/reject on violation in `push_scope`.
5. Keep the 9 existing keys and the id grammar `<op>@<file>:<line>#<counter>[@<scope>]`
   unchanged (AMP's reducer depends on it).

## Phase 3 — Streaming / chunkable-driver contract

Verified internals: journal buffer `inline thread_local std::vector<LogRecord> buf`
(`journal.hpp:56`); id counters thread_local (`:60-69`); scope stack thread_local
(`tracked.hpp:66`); `flush()` truncates (default-mode ofstream) and writes only the calling
thread's buffer (`journal.hpp:143-157`); `clear()` empties buf AND resets the id counters
(`:113-120`); there is no incremental flush of any kind (this is why AMP's 10k run peaks at
~80 GB and why chunk=500 holds ~5 GB in RAM per worker).

Work:
- `journal::flush_and_clear(std::ostream&)` (~15-20 lines): serialize, clear buf + caches,
  **do NOT touch the id counters** (so id sequences match monolithic runs). Emits the schema
  header once per stream (first call).
- Document the **chunkable-driver contract** (library-owned; today it exists only in AMP
  comments): per-unit RNG reseed, `--sample-offset` prefix refill with recordless leaf
  factories (`track()/constant()/literal()` emit no records — verified), per-thread scope
  pushing (main-thread scopes are invisible to workers), one journal stream per thread,
  sample-contiguity within a stream, merge-of-reduces == reduce-of-concatenation.
- Threading guidance doc: id uniqueness across threads comes from the scope suffix — never
  dispatch the same (unit, sample) on two threads.

## Phase 4 — Op-coverage extension

Add `log1p` (κ = |x/((1+x)·ln(1+x))|), `expm1` (κ = |x·e^x/(e^x−1)|), `hypot` (κ ≤ 1),
`fma` to `ops.hpp` with closed-form conds + tests + CONDITION_NUMBERS.md derivations.
Rationale: these are the canonical *fixes* for the cancellation signals the AMP pipeline
hunts; without them a rewritten kernel wraps them opaque (cond=1) and conditioning
improvement cannot be verified (hard prerequisite of AMP Objective 2).

## Phase 5 — Tools migration (from AMP, all verified generic)

Python package `tools/tracked_tools/` (pip-installable, console scripts):

| Tool | Source in AMP | Generalization notes |
|---|---|---|
| `tracked-line-inject` | `agents/tracked_integrator/line_injector.py` (487 ln) | libclang statement-scope injection emitting `line=<file>:<N>` scopes; parameterize include dirs/defines/scope-var name; library now owns the `line=` grammar; basename-collision assumption documented (path-qualified ids deferred — see IMPROVEMENT_PLAN Obj 4.4) |
| `tracked-boundary-patch` | `agents/integrator_base/c8.py` (406 ln) | already type-parameterized (`_TypePatterns` per scalar spelling), zero app identifiers; gcc-only diagnostics documented; exact-once patch discipline kept |
| interop kit (`tracked_tools/interop/`) | ruleset `agents/tracked_integrator/system_prompt.txt` (Rules 1-9 + C1-C7 — the file itself states C1-C7 "hold for ANY target library"), SOURCE_HASH cache `agents/integrator_base/cache.py`, header-embed + streaming `agents/integrator_base/llm.py` | LLM call is a pluggable seam (callable arg); the ruleset text is useful even to a human. `TRACKED_HERE` forwarding convention + the single-default-argument rule move into library docs (mechanism already library-side: trailing `SourceLocation loc = {}` on every named op, `opaque_at`, `TRACKED_HERE` at `journal.hpp:35`) |
| `tracked-reduce` | generic core of `agents/shared/stability_reducer.py` (1039 ln) | port for **byte parity first**: `_read_jsonl` streaming, `_scope_str` id-grammar parsing, `_iter_samples` (sample-contiguity contract), `_analyze_sample` DAG + backward amp pass, `_cond_eff` floor, topo order, cascade-chain extraction, `LogHist`, associative `merge_reports`, versioned shard schema. **Stays in AMP**: U-constants, signal-class taxonomy, `predicted_rel_err_if_*`, `finalize_report`'s strategy-facing shape (injectable policy/config). After parity: the 5.B upgrades (argmax sample identity, argmax amp paths, distribution hists, ops-per-sample) as versioned additions. Note: `journal.hpp:159-215`'s C++ graph helpers are unused by AMP — the reducer duplicates them; long-term the native map-step (Obj 6 stage 3) resolves this |

Differential-test harness: run old (AMP) and new reducers on the same journals; canonical-
JSON compare (parsed values for floats). Merge associativity makes per-chunk equality imply
any-partition equality.

## Phase 6 — Viewer skeleton (`viewer/` + `tracked-view`)

- **Data note (verified)**: the committed AMP fixture journals (`runs/{cancellation,kahan,
  lnrat,cln,log_sum_exp,naive_variance}/journal.jsonl`, ~245 KB each) are pre-v0.3 — no
  `id` field, flat `prov` (`in` holds source names, not record ids) — so they cannot feed
  the DAG view. Do NOT try AMP's `regen_recall.sh` on this Mac (hardcoded cluster paths;
  two fixtures need Kokkos, absent here) — use small in-repo example kernels + a v1-schema
  driver instead, with regenerated fixtures living in THIS repo; AMP stays read-only in
  Session B. First step: regenerate fixture
  journals with the v1 library (fixtures are plain C++; `scripts/regen_recall.sh` in AMP is
  the recipe; a small in-repo example kernel also works and keeps the viewer self-contained).
- `tracked-view <run_dir> -o view.html`: a **distiller** (in `tools/`) producing a
  view-model JSON (target ≪50 MB; for AMP-scale runs: regions minus `prov_vars` ≈ 1-2 MB,
  deduped chains ≈ 1 MB, routing/telemetry/iterations ≈ 100 KB, per-sample digit arrays
  2-4 MB, call graph ≈ 1 MB) + a self-contained static HTML (hand-rolled SVG/canvas, no npm).
- v1 views: (1) **DAG view** — unit → function → region nodes (color = signal class /
  assigned rung, size = flop weight, badges for range-guard/cap), edges = calls + chain
  spans; drill-down to a per-sample op-level DAG (one-sample journal slice: rerun with
  `--sample-count 1 --sample-offset <i>`, ~0.5 MB per unit — bit-identical by the prefix-fill
  contract). (2) **Actionable report** — ranked hotspot cards + the decision ledger
  (admission/prune/verdict reasons that today die in JSON). Apply the dataviz skill when
  writing the actual chart code.
- AMP adapter (strategy/solver decision fields: `tu_rows`, `iterations.jsonl`,
  `*_precise_digits.jsonl`, ManifestRow, `ceiling_regions`) = fast-follow, lives AMP-side.
- AMP serialization gaps the full viewer needs (AMP-side work, tracked in IMPROVEMENT_PLAN):
  persist the call graph (built transiently, never written); dedupe the 264k chain records
  into structural span-set groups; argmax sample identity at reduce time.

## Phase 7 — AMP lockstep switchover

- Point AMP's subtree at the renamed repo (`git subtree pull` with the new URL; the GitHub
  redirect covers the old one; update `README.md:220-221`).
- Reducer: version-aware parsing (v1 header record required; explicit legacy mode for v0.3
  journals); retire the legacy `log_parser.py` per-variable rollup (IMPROVEMENT_PLAN 5.A.1).
- Update `agents/tracked_integrator/` + `agents/integrator_base/{cache,llm,c8}.py` call
  sites to import from `tracked_tools` (keep thin AMP wrappers so the diff stays small).
- Run the acceptance bar (point 14, above). **Then stop and check in before point 15.**

## Ground-truth appendix (for the fresh session — verified 2026-08-20)

- Journal keys: `op,at,id,in,val,cond,rel_err,prov_vars,prov_consts` (`journal.hpp:39-49,
  143-157`). Id grammar: `<op>@<file>:<line>#<counter>[@<scope>]` (`tracked.hpp:80-93`);
  `_lit@` prefix for literals. Leaf factories emit no records (`tracked.hpp:204-240`).
- Error recurrence: `new_err = cond·(max(input_errs) + u)`, `u = epsilon/2`
  (`tracked.hpp:44-47, 316-318`). Conds: add/sub `(|a|+|b|)/|a±b|`; mul/div/neg/abs 1;
  sqrt 0.5; exp |x|; log 1/|ln x| capped 1/u; sin/cos capped; atan2 capped. Known blind
  spots (documented in the library's own docs; do not "fix" silently — they are model
  choices): max-gating underestimates product chains; swamping invisible (the Kahan test
  asserts `max_cond < 2`); opaque cond=1.
- Thread model: everything mutable is `thread_local` (buf `journal.hpp:56`; counters
  `:60-69`; scope stack `tracked.hpp:66`; caches `:72-74`). No data races; no cross-thread
  visibility.
- Reducer contract: `_iter_samples` requires per-stream sample contiguity (batch key = scope
  minus `line=`); region key = `line=` scope value else `at` else `""`;
  `merge([reduce(A), reduce(B)]) == reduce(A ++ B)` (documented associative).
- Sizes: ~10 MB journal per global sample across 21 qcdloop integrals (~0.5 MB per
  (integral, sample), ~930 records); 5k report ≈ 850 MB dominated by `prov_vars` unions +
  non-source `variables{}` + 264,095 per-(sample,victim) chain records — all separable
  (fast_merge's own filters prove it).
- Deferred upstream roadmap already in the library's docs (`docs/PLAN*.md`): ring buffer,
  weighted DAG provenance, GPU/device, higher-order error models, `prov_opaque` — align the
  new roadmap doc with these rather than duplicating.
