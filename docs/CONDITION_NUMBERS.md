# Condition numbers & error propagation — derivations

This document derives every per-op condition number used by `Tracked<T>` from
first principles, states the error-propagation model, and documents the
modeling conventions (and the places where the library deliberately departs
from the strict textbook formula). It also lists the numerical-analysis
literature the results come from — see [References](#references).

These formulas are implemented in `include/tracked/tracked.hpp` (arithmetic)
and `include/tracked/ops.hpp` (math functions). `Complex<T>` has no condition
numbers of its own: every complex op decomposes into the real ops below, so its
conditioning is whatever the journal records for those sub-ops.

---

## 1. Preliminaries

### 1.1 The relative condition number

For a differentiable scalar function `f` of a scalar input `x`, the **relative
condition number** measures how a small *relative* perturbation of the input is
amplified into a *relative* perturbation of the output. Perturb `x → x(1+δ)`
with `|δ| ≪ 1`. To first order,

```
f(x(1+δ)) ≈ f(x) + f'(x)·xδ
```

so the relative change in the output is

```
[f(x(1+δ)) − f(x)] / f(x) ≈ (x f'(x) / f(x)) · δ
```

and the amplification factor — the **relative condition number** — is

```
        | x · f'(x) |
κ(f,x) = | --------- |                                              (1)
        |   f(x)    |
```

This is the quantity `Tracked<T>` records as `cond` for single-input ops.
`κ ≫ 1` means the op is ill-conditioned at that input: it magnifies relative
input error. Bits lost ≈ `log₂(κ)`; decimal digits lost ≈ `log₁₀(κ)`.

### 1.2 Multiple inputs

For `f(x₁,…,xₙ)`, sum the per-input relative contributions (this is the
componentwise relative condition number; it is an upper bound that treats each
input's perturbation as independent and worst-case aligned):

```
        Σᵢ | xᵢ · ∂f/∂xᵢ |
κ(f,x) = -----------------                                          (2)
              | f(x) |
```

`add`/`sub`/`atan2` use this two-input form.

---

## 2. Arithmetic ops

### 2.1 Addition — `add(a,b)`,  `f = a + b`

`∂f/∂a = 1`, `∂f/∂b = 1`. By (2):

```
       |a·1| + |b·1|     |a| + |b|
κ_+  = ------------- =  -----------                                 (3)
          |a + b|        |a + b|
```

Implemented at `tracked.hpp:194`. When `a ≈ −b` the denominator collapses while
the numerator stays large — this is **catastrophic cancellation**, and `κ_+`
blows up exactly there. Edge cases in code: if `a+b = 0` but `|a|+|b| ≠ 0`,
cond is capped at `1/u` (maximal loss); if both are zero, cond = 1.

### 2.2 Subtraction — `sub(a,b)`,  `f = a − b`

`∂f/∂a = 1`, `∂f/∂b = −1`. By (2):

```
       |a| + |−b|       |a| + |b|
κ_−  = ----------- =  -----------                                   (4)
         |a − b|        |a − b|
```

Identical to addition with `b → −b` (`tracked.hpp:211`). This is *the* signature
case: `(1+ε) − 1` has `κ ≈ 2/ε`. See `tests/tracked/test_cancellation.cpp`.

### 2.3 Multiplication — `mul(a,b)`,  `f = a·b` → **library uses κ = 1**

The strict componentwise condition number is **2**:

```
       |a·b| + |b·a|     2|ab|
κ_×  = ------------- =  ------- = 2                                  (5)
           |ab|          |ab|
```

But the library sets `cond = 1` (`tracked.hpp:226`). The reason is the
**max-of-inputs** error model (§4): multiplication does not *amplify* a relative
error — `fl(a·b) = a·b·(1+δ)` adds the relative errors of the operands plus one
rounding, it never magnifies them. The factor of 2 in (5) is the *sum* of two
unit contributions; under the max model each individual contribution has
amplification 1, so multiplication is treated as perfectly well-conditioned
(κ = 1) and the second operand's error is folded in via `max`, not `+`. This is
a deliberate simplification — see §4.1 and the [conventions note](#5-modeling-conventions--departures-from-textbook).

### 2.4 Division — `div(a,b)`,  `f = a/b` → **library uses κ = 1**

`∂f/∂a = 1/b`, `∂f/∂b = −a/b²`. By (2):

```
       |a/b| + |a/b|     2|a/b|
κ_÷  = ------------- =  -------- = 2                                 (6)
          |a/b|          |a/b|
```

Same situation as multiplication: textbook value 2, library uses `cond = 1`
(`tracked.hpp:241`) for the same max-model reason. Division is well-conditioned
in the relative sense everywhere `b ≠ 0`; the danger of division is *overflow*
(handled by Smith's algorithm in complex division), not relative-error
amplification.

### 2.5 Negation — `neg(a)`,  `f = −x`

`f'(x) = −1`. By (1): `κ = |x·(−1)/(−x)| = 1` (`tracked.hpp:254`). Exact
operation; never amplifies.

---

## 3. Math functions (`ops.hpp`)

### 3.1 `abs(x)`,  `f = |x|`

`f'(x) = sign(x)`. By (1): `κ = |x·sign(x)/|x|| = 1` (`ops.hpp:60`).

### 3.2 `sqrt(x)`,  `f = √x`

`f'(x) = 1/(2√x)`. By (1):

```
     | x · 1/(2√x) |     |  √x  |     1
κ  = | ----------- | =  | ---- | =  ---  =  0.5                     (7)
     |     √x      |     | 2√x  |     2
```

Square root **halves** relative error — it is unconditionally well-conditioned
(`ops.hpp:18`). Intuition: a relative perturbation in the radicand is halved in
the root, matching `√(x(1+δ)) ≈ √x·(1+δ/2)`.

### 3.3 `exp(x)`,  `f = eˣ`

`f'(x) = eˣ`. By (1): `κ = |x·eˣ/eˣ| = |x|` (`ops.hpp:30`). Large arguments
amplify error linearly — `exp(50)` has `κ = 50` (~5–6 digits lost). This is
why log-sum-exp must shift by the max before exponentiating
(`test_log_sum_exp.cpp`).

### 3.4 `log(x)`,  `f = ln x`

`f'(x) = 1/x`. By (1):

```
     | x · (1/x) |       1
κ  = | --------- | =  --------                                      (8)
     |   ln x    |     |ln x|
```

Implemented at `ops.hpp:44`. Blows up as `x → 1` (where `ln x → 0`): a tiny
relative error in `x ≈ 1` becomes an unbounded relative error in `ln x ≈ 0`.
**Cap:** when `|ln x| < u`, cond is set to `1/u` to avoid division by ~0.

### 3.5 `sin(x)`,  `f = sin x`

`f'(x) = cos x`. By (1):

```
     | x · cos x |
κ  = | --------- | =  | x · cot x |                                 (9)
     |   sin x   |
```

(`ops.hpp:75`). Blows up near integer multiples of π, where `sin x → 0`. **Cap:**
`1/u` when `|sin x| < u·|x|`. Note the `|x|` factor — this is *relative*
conditioning w.r.t. the argument, so large arguments are also poorly
conditioned (the classic "trig of a huge angle" problem).

### 3.6 `cos(x)`,  `f = cos x`

`f'(x) = −sin x`. By (1):

```
     | x · (−sin x) |
κ  = | ------------ | =  | x · tan x |                             (10)
     |    cos x     |
```

(`ops.hpp:95`). Blows up near `π/2 + kπ`, where `cos x → 0`. **Cap:** `1/u` when
`|cos x| < u·|x|`. At `x = 0`, `κ = |0·tan 0| = 0` exactly (perfectly
conditioned).

### 3.7 `atan2(y, x)`,  `f = atan2(y, x)`

Two-input. The partial derivatives of the angle are

```
∂f/∂y =  x / (x² + y²)
∂f/∂x = −y / (x² + y²)
```

By the two-input form (2):

```
       |y · ∂f/∂y| + |x · ∂f/∂x|     |xy| + |yx|         2|xy|
κ  =  ------------------------- =  ----------------- = ----------------- (11)
              |atan2(y,x)|         (x²+y²)|atan2|      (x²+y²)|atan2(y,x)|
```

(`ops.hpp:116`). Pathological near `atan2 → 0` (the positive real axis) and at
the origin. **Cap:** `1/u` when `|atan2(y,x)| < u`. This formula governs the
imaginary part of `Complex log`, where it makes the branch cut visible.

### 3.8 `log1p(x)`,  `f = ln(1+x)`

`f'(x) = 1/(1+x)`. By (1):

```
     |  x / (1+x)  |            x
κ  = | ----------- | =  ----------------                           (11a)
     |   ln(1+x)   |     (1+x) · ln(1+x)
```

As `x → 0`, `ln(1+x) → x`, so `κ → 1`: **well-conditioned exactly where
computing `log(1+x)` through the add would cancel** (the add `1+x` commits a
relative error ~`u/x` before `log` ever sees it; `log1p` takes `x` directly).
That is why `log1p` is the canonical *fix* for the `log`-near-1 signal. Blows
up only as `x → −1⁺` (the log singularity). **Cap:** `1/u` when
`|(1+x)·ln(1+x)| < u·|x|` (`cap="log1p"`); at `x = 0` the implementation
returns the limit `κ = 1` exactly.

### 3.9 `expm1(x)`,  `f = eˣ − 1`

`f'(x) = eˣ`. By (1):

```
     |  x · eˣ  |     |      (     1 )|
κ  = | -------- |  =  | x ·  ( 1 + - )|                            (11b)
     |  eˣ − 1  |     |      (     f )|
```

(the second form, via `eˣ = f + 1`, is what the implementation computes — it
avoids `∞/∞` for large `x`). As `x → 0`, `κ → 1`: well-conditioned exactly
where `exp(x) − 1` through the sub would cancel — the canonical fix for that
cascade signal. `κ ≈ |x|` for large positive `x` (same growth as `exp`),
`κ → 0` as `x → −∞`. Finite for all finite `x` — **no saturation cap** (only
a NaN input caps, `cap="nan"`).

### 3.10 `hypot(x, y)`,  `f = √(x² + y²)`

Two-input. `∂f/∂x = x/f`, so the per-input relative sensitivities are

```
x · ∂f/∂x       x²                    y²
---------- = -------- ∈ [0, 1],    -------- ∈ [0, 1]
    f         x² + y²               x² + y²
```

They sum to exactly 1, so under the max-gated model (2) the joint bound is
`κ = 1`: **never worse-conditioned than its inputs**, and (as a library
function) computed without the intermediate overflow/underflow that squaring
would commit — the canonical fix for a range-guard violation in `√(x²+y²)`.
Never capped.

### 3.11 `fma(a, b, c)`,  `f = a·b + c` (single rounding)

The multiply is exact inside an fma (one rounding at the end), so the
conditioning is the *add's*, with the product as one addend:

```
      |a·b| + |c|
κ  = -------------                                                 (11c)
      |a·b + c|
```

Cancellation between `a·b` and `c` is fma's hotspot — and fma is also the
canonical *fix* for that cancellation, because the product enters it exactly
(no committed rounding before the subtract; this is how compensated products
and Kahan-style algorithms exploit it). Same special cases as `add`: an exact
zero tie (`a·b == −c`) reports `κ = 1` with `exact_tie`; flush-to-zero
underflow caps at `1/u` (`cap="fma_uflow"`); NaN caps (`cap="nan"`).
*Approximation:* the implementation evaluates (11c) with the **rounded**
product `fl(a·b)` (the exact product is not observable in a double), so in the
rare corner where the exact product ties with `−c` but the rounded one does
not, the resulting exact zero is labeled `fma_uflow` rather than `exact_tie`.

### 3.12 Summary table

| Op | `f` | `κ` (this library) | Strict κ from (1)/(2) | Note |
|---|---|---|---|---|
| `add(a,b)` | `a+b` | `(\|a\|+\|b\|)/\|a+b\|` | same | cancellation |
| `sub(a,b)` | `a−b` | `(\|a\|+\|b\|)/\|a−b\|` | same | cancellation |
| `mul(a,b)` | `a·b` | **1** | 2 | max-model, §2.3 |
| `div(a,b)` | `a/b` | **1** | 2 | max-model, §2.4 |
| `neg(a)`   | `−x` | 1 | 1 | exact |
| `abs(x)`   | `\|x\|` | 1 | 1 | exact |
| `sqrt(x)`  | `√x` | 0.5 | 0.5 | halves error |
| `exp(x)`   | `eˣ` | `\|x\|` | `\|x\|` | |
| `log(x)`   | `ln x` | `1/\|ln x\|` | `1/\|ln x\|` | cap `1/u` |
| `sin(x)`   | `sin x` | `\|x·cot x\|` | same | cap `1/u` |
| `cos(x)`   | `cos x` | `\|x·tan x\|` | same | cap `1/u` |
| `atan2(y,x)`| `atan2` | `2\|xy\|/((x²+y²)\|atan2\|)` | same | cap `1/u` |
| `log1p(x)` | `ln(1+x)` | `\|x/((1+x)·ln(1+x))\|` | same | cap `1/u`; κ→1 at 0 |
| `expm1(x)` | `eˣ−1` | `\|x·eˣ/(eˣ−1)\|` | same | no cap; κ→1 at 0 |
| `hypot(x,y)` | `√(x²+y²)` | **1** | ≤ 1 per input | max-model, §3.10 |
| `fma(a,b,c)` | `a·b+c` | `(\|a·b\|+\|c\|)/\|a·b+c\|` | same | cancellation; §3.11 |

---

## 4. Error-bound propagation

### 4.1 The model

Every op records an accumulated relative-error bound on its output:

```
new_rel_err = local_cond · ( max(input_rel_errs) + unit_roundoff<T>() )   (12)
```

with `unit_roundoff<double>() = ε/2 = 2⁻⁵³ ≈ 1.11e-16` (`tracked.hpp:30`).

Derivation. The IEEE-754 model of a correctly-rounded elementary operation is

```
fl(a op b) = (a op b)(1 + δ),   |δ| ≤ u                            (13)
```

(this is the *standard model* of floating-point arithmetic — Higham §2.2,
"Model of Arithmetic", p. 40; Trefethen & Bau, Lecture 13). The relative error
already present
on the inputs propagates through the op scaled by the op's condition number `κ`,
and the op itself contributes one fresh rounding `δ`. A rigorous first-order
bound is

```
new_rel_err ≲ κ · (input contribution) + u                        (14)
```

Equation (12) makes two deliberate simplifications relative to (14):

1. **`max` instead of `sum` of input errors.** The strict componentwise bound
   would combine the inputs' relative errors additively. Using `max` keeps the
   model cheap and is why `mul`/`div` carry `cond = 1` rather than 2 (§2.3): the
   "2" in the textbook condition number is precisely the *sum* of two unit
   contributions, which the `max` model collapses to a single unit.
2. **Folding the final rounding inside the multiply.** (12) writes
   `κ·(… + u)` rather than `κ·(…) + u`, i.e. the boundary rounding is scaled by
   `κ` too. This is slightly *conservative* (an over-estimate when `κ > 1`),
   which is the safe direction for a diagnostic.

Fresh inputs from `track(id, v)` start at `rel_err = u` (`tracked.hpp:76`), the
rounding incurred representing `v` as a `double`.

### 4.2 What the model does *not* capture

The first-order model intentionally misses **swamping** (a tiny addend lost in a
large running sum): each individual `add` has `κ ≈ 1`, so the journal shows no
amplification even though information is silently dropped. This is by design and
is exactly what `test_kahan_vs_naive.cpp` calibrates — Kahan compensation
*recovers* the lost bits, which paradoxically shows up as *high* cond on the
compensation subtractions (the cancellation is doing useful work).

### 4.3 `max_cond_seen_`

Separately from the error bound, each `Tracked` carries `max_cond_seen_` — the
running maximum of `local_cond` over every op in its dependency chain
(`tracked.hpp:198` and equivalents). This is the "worst conditioning anywhere
upstream" and is what most tests assert on, because it survives even when a
later well-conditioned op would otherwise mask an earlier catastrophe.

---

## 5. Modeling conventions & departures from textbook

| Choice | Strict NA | This library | Why |
|---|---|---|---|
| `mul`/`div` condition | 2 | 1 | `max`-of-inputs error model (§4.1) |
| Input-error combination | sum | `max` | cheap, matches the `cond=1` choice for `×`,`÷` |
| Boundary rounding | `κ·e + u` | `κ·(e + u)` | conservative over-estimate |
| Singular `cond` (`log`,`sin`,`cos`,`atan2`) | `∞` | capped at `1/u` | avoid overflow/NaN; `1/u` = "all bits lost" |
| `opaque` barrier | — | `cond = 1` | no insight into the black box ⇒ conservative pass-through, **not** `0` (which would falsely claim stability — see `CHARACTERIZER_NOTES.md`) |

---

## 6. References

The original design notes (`PLAN.md`, `PLAN-v1.1.md`) describe these as
"standard first-order numerical-analysis results" but cite only Smith (1962),
for the complex-division algorithm. The formulas above are textbook results.

**How to read the per-op attribution (§6.2).** The per-op condition numbers are
*not* each a separate published result with its own citation. They are all one
definition — the relative condition number `κ = |x f'(x)/f(x)|`, eq. (1) — applied
to each elementary function by elementary calculus. So "the source" for, e.g.,
`sqrt → 0.5` is: *the general definition (Trefethen & Bau, Lecture 12; Higham
§1.6, "Conditioning"), evaluated at `f = √x`.* The mapping below points each op at the source of
the **framework** it follows from, and flags the few ops whose specific result
(cancellation, summation, complex division) is called out explicitly in a source.

### 6.1 Bibliography (verified)

All five entries were verified against Crossref DOI metadata (titles, journals,
volumes, issues, years, and pages are as registered). DOIs are included so they
can be re-checked.

- **N. J. Higham, *Accuracy and Stability of Numerical Algorithms*, 2nd ed.,
  SIAM, Philadelphia, 2002.** DOI [10.1137/1.9780898718027](https://doi.org/10.1137/1.9780898718027).
  The standard monograph. Section and page numbers below are from the book's
  own table of contents (2nd ed.):
  - **§1.6 "Conditioning"** (p. 8) — the relative condition number `κ`.
  - **§1.7 "Cancellation"** (p. 9) — catastrophic cancellation; underpins the
    `add`/`sub` condition number (3)/(4).
  - **§1.9 "Computing the Sample Variance"** (p. 11) — the naive-variance
    pathology calibrated by `test_naive_variance.cpp`.
  - **§2.2 "Model of Arithmetic"** (p. 40) — the standard model
    `fl(a op b) = (a op b)(1+δ), |δ| ≤ u` (eq. 13 here).
  - **§2.5 "Exact Subtraction"** (p. 45) — when subtraction is exact (Sterbenz).
  - **§2.10 "Elementary Functions"** (p. 50) — rounding of `sqrt`/`exp`/`log`/trig.
  - **§3.6 "Complex Arithmetic"** (p. 71) — error model for complex ops
    (relevant to `complex.hpp`).
  - **§4.3 "Compensated Summation"** (p. 83) — Kahan summation; the swamping vs
    compensation behavior in §4.2.

- **L. N. Trefethen & D. Bau, III, *Numerical Linear Algebra*, SIAM,
  Philadelphia, 1997.** DOI [10.1137/1.9780898719574](https://doi.org/10.1137/1.9780898719574).
  **Lecture 12, "Conditioning and Condition Numbers"** (p. 89) defines the
  relative condition number `κ = |x f'(x)/f(x)|` (eq. 1 here) and its
  multi-input form (eq. 2); **Lecture 13, "Floating Point Arithmetic"** gives
  the standard model. (Lecture titles verified against the book's published
  table of contents.)

- **D. Goldberg, "What every computer scientist should know about floating-point
  arithmetic," *ACM Computing Surveys* 23(1), 1991, pp. 5–48.** DOI
  [10.1145/103162.103163](https://doi.org/10.1145/103162.103163). Accessible
  derivation of catastrophic cancellation and the relative-error model;
  motivates the `add`/`sub` condition number (3)/(4).

- **R. L. Smith, "Algorithm 116: Complex division," *Communications of the ACM*
  5(8), 1962, p. 435.** DOI
  [10.1145/368637.368661](https://doi.org/10.1145/368637.368661). The scaled
  algorithm used in `complex.hpp` `operator/` — the *only* citation present in
  the original sources. (Conditioning of complex division is still recovered
  from the real sub-ops; Smith's contribution is overflow-avoidance, not
  conditioning.)

- **W. Kahan, "Pracniques: further remarks on reducing truncation errors,"
  *Communications of the ACM* 8(1), 1965, p. 40.** DOI
  [10.1145/363707.363723](https://doi.org/10.1145/363707.363723). Compensated
  summation, the algorithm calibrated by `test_kahan_vs_naive.cpp` (§4.2).

### 6.2 Per-op source mapping

| Op | Formula here | Follows from | Specific source callout |
|---|---|---|---|
| `add`/`sub` | `(\|a\|+\|b\|)/\|a±b\|` | (2) with `f=a±b` | cancellation: Higham §1.7; Goldberg §"Cancellation" |
| `mul`/`div` | 1 *(textbook 2)* | (2) with `f=ab`, `a/b`; reduced to 1 by the max-model (§2.3, §4.1) | model choice is this library's, not a citation |
| `neg`/`abs` | 1 | (1) with `f=−x`, `\|x\|` | — |
| `sqrt` | 0.5 | (1) with `f=√x` | T&B Lecture 12 framework |
| `exp` | `\|x\|` | (1) with `f=eˣ` | T&B Lecture 12 framework |
| `log` | `1/\|ln x\|` | (1) with `f=ln x` | T&B Lecture 12 framework |
| `sin` | `\|x·cot x\|` | (1) with `f=sin x` | T&B Lecture 12 framework |
| `cos` | `\|x·tan x\|` | (1) with `f=cos x` | T&B Lecture 12 framework |
| `atan2` | `2\|xy\|/((x²+y²)\|atan2\|)` | (2) with the `atan2` partials (§3.7) | T&B Lecture 12 framework |
| `log1p` | `\|x/((1+x)·ln(1+x))\|` | (1) with `f=ln(1+x)` (§3.8) | T&B Lecture 12 framework; motivation: Goldberg §"Cancellation" |
| `expm1` | `\|x·eˣ/(eˣ−1)\|` | (1) with `f=eˣ−1` (§3.9) | T&B Lecture 12 framework |
| `hypot` | 1 *(per-input ≤ 1)* | (2) with `f=√(x²+y²)` (§3.10) | Higham §27.5 (hypot without overflow) |
| `fma` | `(\|a·b\|+\|c\|)/\|a·b+c\|` | (2), exact product + one rounding (§3.11) | Higham §2.6 (fused multiply-add) |
| error model (12) | `κ·(max_err + u)` | standard model `fl=(a op b)(1+δ)` | Higham §2.2; T&B Lecture 13 |
| complex `/` | Smith branches | overflow avoidance, not conditioning | **Smith 1962**; Higham §3.6 |
| Kahan test | compensated sum | swamping vs compensation (§4.2) | **Kahan 1965**; Higham §4.3 |

The general framework — eqs. (1), (2), (12) — is Trefethen & Bau (Lectures 12–13)
and Higham (§1.6 "Conditioning" and §2.2 "Model of Arithmetic"). Every
single-input op (`sqrt`/`exp`/`log`/`abs`/`neg`/`sin`/`cos`) is that framework
evaluated at the named `f`; the derivations are in §2–§3 above. Only three
results have a *dedicated* source: cancellation (Higham §1.7 / Goldberg),
complex division (Smith), and compensated summation (Higham §4.3 / Kahan).
