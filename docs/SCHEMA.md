# Tracked journal schema (v1)

The JSONL journal is the library's **public contract**: every downstream tool
(`tracked-reduce`, the viewer distiller, any consumer's parser) reads it. This
document is normative. The C++ macro `TRACKED_JOURNAL_SCHEMA_VERSION`
(`include/tracked/version.hpp`) states the schema a library build emits.

## File shape

A journal is UTF-8 JSON Lines. **Line 1 of every v1 journal is a header
record**; every subsequent line is one op record.

### Header record

```json
{"schema": 1, "library_version": "1.0.0", "keys": ["op","at","id","in","val","cond","rel_err","prov_vars","prov_consts"]}
```

- `schema` — integer schema version. Readers MUST check it: hard-require a
  known version, or refuse.
- `library_version` — `TRACKED_VERSION_STRING` of the emitting build
  (informational).
- `keys` — the keys guaranteed present on **every** op record, in emission
  order. Optional keys (below) are not listed.

**Reader rule:** if line 1 has no `schema` key, the file is a legacy
(pre-v1, "v0.3") journal — headerless, NaN→`null`, ±Inf clamped. Readers MUST
NOT silently guess: they either hard-fail or are *explicitly* put in legacy
mode by their caller (e.g. `tracked-reduce --legacy` /
`reduce_journal(..., legacy=True)`).

**Concatenation rule:** concatenating v1 journals (`cat a.jsonl b.jsonl`) is a
supported way to combine shards **only when the shards have disjoint sample
scopes** (the chunkable-driver contract: id counters are per-thread and reset
per run, so id uniqueness across shards comes solely from the scope suffix —
concatenating shards that reuse a scope collides ids into one false DAG, and
concatenating unscoped shards fuses their boundary batches). Under that
precondition, readers MUST treat a header-shaped record (any record carrying a
`schema` key) found **mid-stream** as a shard boundary: validate it and skip
it **before** any sample grouping (a header is not an op record and must not
fragment or terminate a batch). All headers in one stream MUST carry the same
`schema` — a reader that finds a differing mid-stream `schema` MUST hard-fail;
`library_version` MAY differ across shards (informational — merge tooling
SHOULD surface skew). With the precondition met, this preserves the reducer
contract `merge([reduce(A), reduce(B)]) == reduce(A ++ B)`.

**Mixed-version rule:** legacy (headerless) and v1 shards MUST NOT be
concatenated or reduced together in one read. A v1 reader MUST hard-fail on a
`null` (or any non-sentinel string) in `val`/`cond`/`rel_err` — that is legacy
contamination, not data. A reader in legacy mode MUST hard-fail on any record
carrying a `schema` key — that is v1 contamination.

### Op record — guaranteed keys (unchanged from v0.3)

```json
{"op":"sub","at":"kernel.cpp:solve:10","id":"sub@kernel.cpp:10#1@integral=B1/sample=0",
 "in":["a","b"],"val":1e-12,"cond":2e12,"rel_err":2.2e-4,
 "prov_vars":["a","b"],"prov_consts":[]}
```

| Key | Type | Meaning |
|---|---|---|
| `op` | string | op name (`add`,`sub`,`mul`,`div`,`neg`,`sqrt`,`exp`,`log`,`abs`,`sin`,`cos`,`atan2`,`opaque`, …) |
| `at` | string | `file:function:line` of a located named call, `""` otherwise |
| `id` | string | stable id of the produced value (grammar below) |
| `in` | [string] | direct-operand ids, verbatim (for `opaque`: `[fn_name, input_ids...]`) |
| `val` | number† | output value |
| `cond` | number† | local condition number of this op |
| `rel_err` | number† | accumulated relative-error bound on the output |
| `prov_vars` | [string] | source-variable roots feeding this value |
| `prov_consts` | [string] | named constants feeding this value |

† or a non-finite sentinel string, see below.

### Op record — optional keys (v1 additions)

Emitted only when applicable; absence is the common case. Readers MUST ignore
op-record keys they do not recognize (additive keys never bump the schema).

- **`cap`** (string) — present iff the emitting op **took a saturating
  branch** (reported `cond = 1/u` instead of computing it). Presence is
  **branch-authoritative**: readers MUST NOT infer capping from the numeric
  `cond` value — an uncapped op's measured cond can coincidentally equal
  `1/u`, and using `cap` (not the number) also makes readers independent of
  `T`'s unit roundoff (`1/u` is `2^53` for `double` but `2^24` for `float`).
  The value names the **cause**, which the number alone cannot distinguish:

  | value | cause |
  |---|---|
  | `"log"` | `log(x)` with `x ≈ 1` (`\|ln x\| ≤ u`), finite result |
  | `"sin"` | `sin(x)` near a multiple of π (`\|sin x\| < u·\|x\|`), finite result |
  | `"cos"` | `cos(x)` near π/2 + kπ (`\|cos x\| < u·\|x\|`), finite result |
  | `"atan2"` | `atan2(y,x)` with `\|result\| < u`, finite result |
  | `"add_uflow"` | `a + b` flushed to exactly 0 from unequal, finite inputs |
  | `"sub_uflow"` | `a - b` flushed to exactly 0 with `a ≠ b`, finite inputs |
  | `"log1p"` | `log1p(x)` with `x ≈ -1` (the log singularity), finite result |
  | `"fma_uflow"` | `fma(a,b,c)` flushed to exactly 0 without an exact tie |
  | `"nan"` | the op produced NaN (NaN operands fall through every saturation guard, so the cond is reported saturated with this cause rather than a misleading one) |

  Note on the `*_uflow` causes: under default IEEE round-to-nearest with
  gradual underflow, `fl(a±b) == 0` for finite doubles happens only when the
  exact result is 0, so these branches are reachable for finite inputs only
  under hardware flush-to-zero (FTZ) modes.

  The set is **open**: later library versions may add values (e.g. `"log1p"`)
  without a schema bump. Readers must treat unknown `cap` values as "capped,
  cause unrecognized", never as an error.

- **`exact_tie`** (boolean, always `true` when present) — present iff an
  add/sub took an **exact-cancellation / zero-tie** branch on **finite**
  operands, reporting `cond = 1`: `a + (-a)` with `a ≠ 0`, `a - a`, or a
  both-operands-zero add. The result is exactly ±0 with no precision lost; the
  marker separates "benign exact tie" from "genuinely well-conditioned op"
  without re-deriving the operand relationship. Non-finite ties (`inf + -inf`,
  `inf - inf`) are NOT marked — they produce NaN and are reported saturated
  with `cap:"nan"` (a v1 behavior fix; pre-v1 libraries reported `cond = 1`
  for them).

### Non-finite encoding (v1, breaking)

`val`, `cond`, and `rel_err` are JSON numbers when finite. Non-finite values
are encoded as the **string sentinels** `"nan"`, `"inf"`, `"-inf"` —
exact, lowercase, case-sensitive, and exclusive (JSON-legal, unambiguous,
lossless).

Reader rules — the requirement is **semantic**, not just parsing (a
`float("inf")`-style coercion "handles" the sentinel while silently destroying
its meaning):

1. A v1 reader MUST accept exactly these three strings in `val`/`cond`/
   `rel_err` and MUST reject any other string (`"NaN"`, `"Infinity"`,
   `"+inf"`, `"1.5"`, …) and any `null` (legacy contamination) as malformed.
2. A reader MUST NOT treat a non-finite measurement as benign. Dropping a
   non-finite `cond`/`rel_err` from aggregation (or coercing it toward 0)
   reports an exploding computation as *stable* — the exact fail-open
   inversion this encoding exists to kill. Baseline conforming behavior:
   treat non-finite `cond`/`rel_err` as **maximally unstable** (clamp to the
   alarm direction and/or count and surface them), and treat a record with
   non-finite `val` as **outside every finite value-range guard** (range-
   unsafe), never as range-neutral.

Legacy (pre-v1) encoding, for readers in legacy mode: NaN→`null`,
+Inf→`1.7976931348623157e+308`, −Inf→`-1.7976931348623157e+308` (clamped,
indistinguishable from a genuine DBL_MAX). Note the legacy clamp was
*conservative* for `cond`/`rel_err` (a huge finite number raises alarms); the
legacy NaN→`null`→0.0 path was the silent-corruption side.

### String escaping (v1, breaking)

The emitter JSON-escapes **every** string field (`op`, `at`, `id`, each `in`
element, `prov_vars`, `prov_consts`, `cap`, header fields): `"`, `\`, and
control characters are emitted as standard JSON escapes. (Pre-v1 emitters
wrote strings raw, so a user-chosen name containing a quote could forge JSON
structure — a silent-corruption hole, closed here.)

User-chosen names (`track()`/`constant()`/`opaque` fn names) may therefore
contain any characters. They SHOULD avoid `#` and `@`, which are load-bearing
in the generated-id grammar: consumers extract the scope of an id by splitting
at the first `#` and the following `@`, so a name like `a#1@k=v` parses as if
it carried a scope.

## Id grammar (unchanged from v0.3)

```
<op>@<file>:<line>#<counter>[@<scope>]     located named call
<op>@?#<counter>[@<scope>]                 operator form (no source location)
<name>                                     source variable / named constant
_lit@<file>:<line>#<n>[@<scope>]           located literal
_lit@?#<n>[@<scope>]                       anonymous literal
```

The `#<counter>` is per-(file, line, op), thread-local, reset by
`journal::clear()`. The scope suffix follows the **first** `#`'s next `@`.
Consumers parse the scope by splitting the id at the first `#`, then taking
everything after the following `@`.

## Scope grammar (v1: documented and validated)

A scope is a stack of `key=value` components joined by `/`, appended to ids as
`@<k1>=<v1>/<k2>=<v2>`. Because `/` joins components and `=` splits key from
value:

- a component MUST match `key=value` with a **non-empty key and non-empty
  value** (`k=`, `=v`, and `kv` are all invalid);
- neither key nor value may contain `/` or `=` (`:` and other characters are
  legal — the emitter JSON-escapes strings, see above);
- `line=` is **reserved** for statement regions: `line=<file>:<N>` (a bare
  basename and a decimal line — a path would contain `/`). Tools treat it
  specially: it is excluded from sample identity and is the primary
  code-region key.

`push_scope` / the `scope` RAII class **validate on push**: a malformed
component throws `std::invalid_argument` (all build types — the check is a
short string scan, and a malformed scope silently corrupts every downstream
grouping, which is strictly worse than failing fast at the push site).

Reserved keys today: `line=`. Well-known conventional keys: `integral=`
(unit bucket; historical spelling kept for compatibility), `sample=`
(sample index).

## Versioning policy

- The record keys `op,at,id,in,val,cond,rel_err,prov_vars,prov_consts` and the
  id grammar are stable within schema 1.
- **Additive** optional record keys and new `cap` values do not bump the
  schema.
- Any change to guaranteed keys, key meaning, the id grammar, or the
  non-finite encoding bumps `schema` (and readers hard-fail on unknown
  versions).

## History

- **v0.2** — flat `prov` set, no `id`.
- **v0.3** — per-value `id`, `in` = direct-operand ids, `prov` split into
  `prov_vars`/`prov_consts` (see `docs/PROVENANCE.md`). Headerless.
- **v0.4** — `literal()` ids (`_lit@…`); record shape unchanged.
- **v1** — header record, `cap`, `exact_tie`, non-finite string sentinels,
  emitter-side JSON escaping, validated scope grammar, and one behavior fix:
  non-finite add/sub ties (`inf + -inf`, `inf - inf`) report a saturated cond
  with `cap:"nan"` instead of pre-v1's `cond = 1`. Everything else
  byte-identical to v0.3.
