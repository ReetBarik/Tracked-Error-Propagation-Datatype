# Tracked — numerical sensitivity instrumentation for C++

`tracked::Tracked<T>` is a header-only, host-only C++17 wrapper around a
floating-point type (primarily `double`). As you do arithmetic and call math
functions on it, it propagates — alongside the value — a first-order
**relative-error bound** and the **condition number** of every operation, and
records each op to a structured JSONL journal.

Replace `double` with `tracked::Tracked<double>` in a computation and the
journal shows you *where* accuracy is lost: catastrophic cancellation, swamping,
ill-conditioned branches, and the like. There is no GPU, CUDA, quadmath, or
extended-precision machinery here — this is a diagnostics tool, not a
higher-precision number type.

## Quick start

```cpp
#include <tracked/tracked.hpp>
#include <tracked/ops.hpp>
#include <tracked/journal.hpp>

using tracked::track;
using tracked::constant;

int main() {
    auto a  = track("a", 1.0 + 1e-12);    // source variable  → prov_vars
    auto b  = track("b", 1.0);            // source variable  → prov_vars
    auto pi = constant("pi", 3.14159);    // named constant    → prov_consts

    auto d = a - b;                       // catastrophic cancellation
    // d.value() ≈ 1e-12, but d.max_cond() is huge → accuracy warning

    tracked::journal::flush("run.jsonl"); // one JSONL record per op
}
```

Three factories complete the role taxonomy — pick by what the value *is*:

| Factory              | Role             | Attribution      | Provenance    |
| -------------------- | ---------------- | ---------------- | ------------- |
| `track(name, v)`     | source variable  | attribution root | `prov_vars`   |
| `constant(name, v)`  | named constant   | audit-only       | `prov_consts` |
| `literal(v [, loc])` | anonymous scalar | auto-id, neither | (neither)     |

`track()` seeds `prov_vars` (reported as the origin of a value); `constant()`
seeds `prov_consts` (recorded for audit, never reported as an origin);
`literal()` promotes a bare scalar with an auto-generated `_lit@…` id but no
provenance role — reach for it only when a scratch scalar genuinely doesn't
warrant a name. See [docs/PROVENANCE.md](docs/PROVENANCE.md).

The journal (schema **v1**, normative spec: [docs/SCHEMA.md](docs/SCHEMA.md))
starts with a header record and has one record per op:

```json
{"schema":1,"library_version":"1.0.0","keys":["op","at","id","in","val","cond","rel_err","prov_vars","prov_consts"]}
{"op":"sub","at":"main.cpp:main:10","id":"sub@main.cpp:10#1",
 "in":["a","b"],"val":1e-12,"cond":2e12,"rel_err":2.2e-4,
 "prov_vars":["a","b"],"prov_consts":[]}
```

Every value carries a stable `id`; `in` lists the **ids of the direct
operands** verbatim, so the journal is a walkable DAG. `cond` is the local
condition number of the op; bits lost ≈ `log2(cond)`, decimal digits lost ≈
`log10(cond)`. Saturated records carry a `cap` cause, exact-cancellation ties
carry `exact_tie`, and NaN/±Inf encode losslessly as `"nan"`/`"inf"`/`"-inf"`.

## Provenance and attribution

Every `Tracked` value has a stable `TrackedId`: source variables and constants
use their given name; derived values get `<op>@<file>:<line>#<counter>[@<scope>]`.
Because `in` records operand ids directly, the library can answer attribution
questions by walking the graph:

```cpp
namespace tracked::journal {
  std::vector<std::string> parents(std::string_view id);        // direct operands
  std::set<std::string>    trace_sources(std::string_view id);  // variable roots that fed it
  std::vector<std::string> trace_ancestors(std::string_view id);// full causal order
}
```

Optionally scope generated ids with an RAII helper — handy for per-sample or
per-iteration attribution:

```cpp
for (int s = 0; s < N; ++s) {
    tracked::scope samp("sample=" + std::to_string(s));   // ids get "@sample=<s>"
    ...
}
```

Full design rationale, the id scheme, scope semantics, the graph model, and
migration notes from v0.2 are in [docs/PROVENANCE.md](docs/PROVENANCE.md).

## Install & consume

The library is header-only C++17; there are three ways to consume it.

**1. Install + `find_package`:**

```bash
cmake -B build -DTRACKED_BUILD_TESTS=OFF
cmake --install build --prefix /your/prefix
```

```cmake
find_package(tracked 1.0 CONFIG REQUIRED)
target_link_libraries(your_target PRIVATE tracked::tracked)
```

**2. `add_subdirectory` / FetchContent:** the test suite (and its Catch2
network fetch) is off by default when the project is not top-level
(`TRACKED_BUILD_TESTS`), so embedding is offline-safe. Link `tracked::tracked`.

**3. Vendored headers (git subtree / copy):** add `include/` to your include
path. `include/tracked/version.hpp` is committed (not configure-generated), so
version macros work without CMake.

## Build & test

No external dependencies beyond a C++17 compiler and CMake (Catch2 is fetched
automatically):

```bash
cmake -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build -j$(nproc)
cd build && ctest --output-on-failure
```

CI runs the suite on ubuntu + macos (`.github/workflows/ci.yml`).

## What's included

- **`include/tracked/`** — the library:
  - `tracked.hpp` — `Tracked<T>`, `track()`, `constant()`, `literal()`, `scope`,
    `opaque()`, arithmetic.
  - `ops.hpp` — `sqrt`, `exp`, `log`, `abs`, `sin`, `cos`, `atan2`, `log1p`,
    `expm1`, `hypot`, `fma` with
    per-op condition numbers.
  - `complex.hpp` — `tracked::Complex<T>`, full complex math decomposed into
    real ops so each is visible in the journal.
  - `journal.hpp` — the JSONL journal (schema in [docs/SCHEMA.md](docs/SCHEMA.md)),
    incremental `flush_and_clear` for chunked drivers
    ([docs/STREAMING.md](docs/STREAMING.md)), and graph-walk helpers
    (`tracked::journal` namespace).
- **`tests/tracked/`** — Catch2 calibration suite: each test drives a known
  numerical pathology and asserts `Tracked<T>` surfaces it.
- **`examples/complex_log_micro/`** — optional Kokkos-based micro-driver
  demonstrating the **opaque-barrier** pattern for kernels that call framework
  math you can't see into. Not built by the top-level CMake; see its README.
- **`examples/viewer_kernels/`** — nine small example kernels + a driver
  written to the chunkable-driver contract; generates the committed viewer
  fixtures (`viewer/fixtures/`) and the viewer's per-sample drill-down slices.
- **`viewer/`** — the run viewer: `tracked-view <run_dir> -o view.html`
  distills a journal run into a zero-dependency single-file HTML with a
  unit → function → region DAG (per-sample op-level drill-down) and an
  actionable report (ranked hotspots + decision ledger). Open
  `viewer/demo/view.html` for a committed demo; see `viewer/README.md`.
- **`docs/`** — design records (`PLAN.md`, `PLAN-v1.1.md`, `PLAN-v0.3.md`,
  `PLAN-v0.4.md`), `PROVENANCE.md` (the provenance model, incl. literals), and
  `CONDITION_NUMBERS.md` (full derivations of every per-op condition number, the
  error-propagation model, and the numerical-analysis references).

## Key ideas

- **Condition number is the only signal.** The library doesn't hardcode a
  "too unstable" threshold — consumers apply their own based on their precision
  needs.
- **Provenance is two categories, not one flat set.** `track()` seeds
  `prov_vars` (attribution roots); `constant()` seeds `prov_consts` (audit-only);
  `literal()` seeds neither but still gets an id so the graph stays walkable.
  Every value has a stable `id`, and `in` records operand ids directly, so
  attribution is a graph walk (`trace_sources`), not a heuristic guess.
- **Error model** (first-order):
  `new_rel_err = local_cond · (max(input_rel_errs) + unit_roundoff<T>())`,
  with `unit_roundoff<double>() = 2⁻⁵³ ≈ 1.11e-16`.
- **Opaque barriers** let you wrap a call that crosses a non-`Tracked`
  boundary (a vendor/framework math function): the interior is hidden as a
  single record, but error and provenance still propagate through.
- **Complex is two tracked reals.** `Complex<T>` emits no records of its own;
  every complex op decomposes into real ops, so you see each underlying
  `add`/`mul`/`sqrt`/etc. A complex-op view is recovered by grouping the
  journal by source location.

## License

MIT — see [LICENSE](LICENSE).

The condition-number and error-propagation formulas are standard first-order
numerical-analysis results; `docs/CONDITION_NUMBERS.md` derives each one and
cites the literature (Higham; Trefethen & Bau; Goldberg; Smith; Kahan).
