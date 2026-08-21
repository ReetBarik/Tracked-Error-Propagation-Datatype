#pragma once
// Math function overloads for Tracked<T>: sqrt, exp, log, abs, sin, cos,
// atan2, log1p, expm1, hypot, fma.
// Condition numbers are closed-form per-op values from numerical analysis.
//
// Found via ADL: write sqrt(x), exp(x), etc. for x of type Tracked<T>.
// For source location capture, pass TRACKED_HERE explicitly:
//   auto y = tracked::sqrt(x, TRACKED_HERE);
//
// v0.3: each op generates its own value id, propagates prov_vars / prov_consts
// separately, and emits with the id + direct-operand-ids signature.

#include <tracked/tracked.hpp>
#include <cmath>

namespace tracked {

// sqrt(x): cond = 0.5 (relative error in output is half that of input)
template <class T>
Tracked<T> sqrt(const Tracked<T>& a, SourceLocation loc = {}) {
    using std::sqrt;
    T res      = sqrt(a.value_);
    T cond     = T(0.5);
    T new_err  = cond * (a.rel_err_bound_ + unit_roundoff<T>());
    T new_cond = std::max(a.max_cond_seen_, cond);
    TrackedId id = detail::make_id("sqrt", loc);
    auto pv = a.prov_vars_;
    auto pc = a.prov_consts_;
    journal::emit("sqrt", loc, id, {a.id_},
        (double)res, (double)cond, (double)new_err, pv, pc);
    return Tracked<T>(res, new_err, new_cond, std::move(id), std::move(pv), std::move(pc));
}

// exp(x): cond = |x|  (large |x| amplifies input error)
template <class T>
Tracked<T> exp(const Tracked<T>& a, SourceLocation loc = {}) {
    using std::exp;
    T res      = exp(a.value_);
    T cond     = std::abs(a.value_);
    T new_err  = cond * (a.rel_err_bound_ + unit_roundoff<T>());
    T new_cond = std::max(a.max_cond_seen_, cond);
    TrackedId id = detail::make_id("exp", loc);
    auto pv = a.prov_vars_;
    auto pc = a.prov_consts_;
    journal::emit("exp", loc, id, {a.id_},
        (double)res, (double)cond, (double)new_err, pv, pc);
    return Tracked<T>(res, new_err, new_cond, std::move(id), std::move(pv), std::move(pc));
}

// log(x): cond = 1/|log(x)|  (blows up near x=1 where log(x)→0)
template <class T>
Tracked<T> log(const Tracked<T>& a, SourceLocation loc = {}) {
    using std::log;
    T res     = log(a.value_);
    T ln_abs  = std::abs(res);
    // Cap condition at 1/u when |log(x)| is smaller than u (x ≈ 1). A NaN
    // result (log of a negative) falls into the cap branch too — the ordinary
    // FP comparison is false for NaN — and is labeled cap="nan", not "log".
    T cond;
    journal::EmitFlags flags;
    if (ln_abs > unit_roundoff<T>()) {
        cond = T(1) / ln_abs;
    } else {
        cond = T(1) / unit_roundoff<T>();
        flags.cap = std::isnan((double)res) ? "nan" : "log";
    }
    T new_err  = cond * (a.rel_err_bound_ + unit_roundoff<T>());
    T new_cond = std::max(a.max_cond_seen_, cond);
    TrackedId id = detail::make_id("log", loc);
    auto pv = a.prov_vars_;
    auto pc = a.prov_consts_;
    journal::emit("log", loc, id, {a.id_},
        (double)res, (double)cond, (double)new_err, pv, pc, flags);
    return Tracked<T>(res, new_err, new_cond, std::move(id), std::move(pv), std::move(pc));
}

// abs(x): cond = 1  (no amplification)
template <class T>
Tracked<T> abs(const Tracked<T>& a, SourceLocation loc = {}) {
    using std::abs;
    T res      = abs(a.value_);
    T cond     = T(1);
    T new_err  = cond * (a.rel_err_bound_ + unit_roundoff<T>());
    T new_cond = std::max(a.max_cond_seen_, cond);
    TrackedId id = detail::make_id("abs", loc);
    auto pv = a.prov_vars_;
    auto pc = a.prov_consts_;
    journal::emit("abs", loc, id, {a.id_},
        (double)res, (double)cond, (double)new_err, pv, pc);
    return Tracked<T>(res, new_err, new_cond, std::move(id), std::move(pv), std::move(pc));
}

// sin(x): cond = |x·cos(x)/sin(x)| = |x·cot(x)|
// Blows up near integer multiples of π. Capped at 1/u when |sin(x)| < u·|x|.
template <class T>
Tracked<T> sin(const Tracked<T>& a, SourceLocation loc = {}) {
    using std::sin; using std::cos; using std::abs;
    T res    = sin(a.value_);
    T u      = unit_roundoff<T>();
    T abs_x  = abs(a.value_);
    T abs_s  = abs(res);
    T cond;
    journal::EmitFlags flags;
    if (abs_s >= u * abs_x) {
        cond = abs_x * abs(cos(a.value_)) / abs_s;
    } else {
        cond = T(1) / u;
        flags.cap = std::isnan((double)res) ? "nan" : "sin";
    }
    T new_err  = cond * (a.rel_err_bound_ + u);
    T new_cond = std::max(a.max_cond_seen_, cond);
    TrackedId id = detail::make_id("sin", loc);
    auto pv = a.prov_vars_;
    auto pc = a.prov_consts_;
    journal::emit("sin", loc, id, {a.id_},
        (double)res, (double)cond, (double)new_err, pv, pc, flags);
    return Tracked<T>(res, new_err, new_cond, std::move(id), std::move(pv), std::move(pc));
}

// cos(x): cond = |x·sin(x)/cos(x)| = |x·tan(x)|
// Blows up near π/2 + kπ. Capped at 1/u when |cos(x)| < u·|x|.
template <class T>
Tracked<T> cos(const Tracked<T>& a, SourceLocation loc = {}) {
    using std::sin; using std::cos; using std::abs;
    T res    = cos(a.value_);
    T u      = unit_roundoff<T>();
    T abs_x  = abs(a.value_);
    T abs_c  = abs(res);
    T cond;
    journal::EmitFlags flags;
    if (abs_c >= u * abs_x) {
        cond = abs_x * abs(sin(a.value_)) / abs_c;
    } else {
        cond = T(1) / u;
        flags.cap = std::isnan((double)res) ? "nan" : "cos";
    }
    T new_err  = cond * (a.rel_err_bound_ + u);
    T new_cond = std::max(a.max_cond_seen_, cond);
    TrackedId id = detail::make_id("cos", loc);
    auto pv = a.prov_vars_;
    auto pc = a.prov_consts_;
    journal::emit("cos", loc, id, {a.id_},
        (double)res, (double)cond, (double)new_err, pv, pc, flags);
    return Tracked<T>(res, new_err, new_cond, std::move(id), std::move(pv), std::move(pc));
}

// atan2(y, x): cond = 2·|x·y| / ((x²+y²)·|atan2(y,x)|)
// Pathological near atan2→0 (positive real axis) and at origin.
// Capped at 1/u when |atan2(y,x)| < u.
template <class T>
Tracked<T> atan2(const Tracked<T>& y, const Tracked<T>& x, SourceLocation loc = {}) {
    using std::atan2; using std::abs;
    T res      = atan2(y.value_, x.value_);
    T u        = unit_roundoff<T>();
    T abs_res  = abs(res);
    T denom    = (x.value_ * x.value_ + y.value_ * y.value_) * abs_res;
    T numer    = T(2) * abs(x.value_ * y.value_);
    T cond;
    journal::EmitFlags flags;
    if (abs_res >= u) {
        cond = numer / denom;
    } else {
        cond = T(1) / u;
        flags.cap = std::isnan((double)res) ? "nan" : "atan2";
    }
    T max_in_err = std::max(y.rel_err_bound_, x.rel_err_bound_);
    T new_err    = cond * (max_in_err + u);
    T new_cond   = std::max({y.max_cond_seen_, x.max_cond_seen_, cond});
    TrackedId id = detail::make_id("atan2", loc);
    auto pv      = detail::prov_union(y.prov_vars_,   x.prov_vars_);
    auto pc      = detail::prov_union(y.prov_consts_, x.prov_consts_);
    journal::emit("atan2", loc, id, {y.id_, x.id_},
        (double)res, (double)cond, (double)new_err, pv, pc, flags);
    return Tracked<T>(res, new_err, new_cond, std::move(id), std::move(pv), std::move(pc));
}

// log1p(x): f = ln(1+x).  κ = |x / ((1+x)·ln(1+x))| → 1 as x → 0 — the whole
// point of log1p: well-conditioned exactly where computing log(1+x) through
// the add would cancel.  Blows up near x = −1 (the log singularity); capped
// at 1/u there (cap="log1p").  See docs/CONDITION_NUMBERS.md §3.8.
template <class T>
Tracked<T> log1p(const Tracked<T>& a, SourceLocation loc = {}) {
    using std::log1p; using std::abs;
    T res = log1p(a.value_);
    T u   = unit_roundoff<T>();
    T cond;
    journal::EmitFlags flags;
    if (a.value_ == T(0)) {
        cond = T(1);                       // exact: log1p(0) = 0; κ → 1
    } else if (std::isnan((double)res)) {  // x < −1
        cond = T(1) / u;
        flags.cap = "nan";
    } else {
        T ax    = abs(a.value_);
        T denom = abs((T(1) + a.value_) * res);
        if (denom >= u * ax) {
            cond = ax / denom;
        } else {                           // x ≈ −1: κ ≥ 1/u
            cond = T(1) / u;
            flags.cap = "log1p";
        }
    }
    T new_err  = cond * (a.rel_err_bound_ + u);
    T new_cond = std::max(a.max_cond_seen_, cond);
    TrackedId id = detail::make_id("log1p", loc);
    auto pv = a.prov_vars_;
    auto pc = a.prov_consts_;
    journal::emit("log1p", loc, id, {a.id_},
        (double)res, (double)cond, (double)new_err, pv, pc, flags);
    return Tracked<T>(res, new_err, new_cond, std::move(id), std::move(pv), std::move(pc));
}

// expm1(x): f = eˣ − 1.  κ = |x·eˣ/(eˣ−1)| = |x·(1 + 1/f)| → 1 as x → 0
// (well-conditioned where exp(x)−1 via the sub would cancel), ~|x| for large
// x, → 0 as x → −∞.  Finite for all finite x — no saturation cap; only a NaN
// input caps (cap="nan").  See docs/CONDITION_NUMBERS.md §3.9.
template <class T>
Tracked<T> expm1(const Tracked<T>& a, SourceLocation loc = {}) {
    using std::expm1; using std::abs;
    T res = expm1(a.value_);
    T u   = unit_roundoff<T>();
    T cond;
    journal::EmitFlags flags;
    if (a.value_ == T(0)) {
        cond = T(1);                       // exact: expm1(0) = 0; κ → 1
    } else if (std::isnan((double)res)) {
        cond = T(1) / u;
        flags.cap = "nan";
    } else if (std::isinf((double)a.value_) && a.value_ < T(0)) {
        cond = T(0);                       // κ = |x·eˣ| → 0 as x → −∞
    } else {
        // κ via eˣ = f + 1, avoiding inf/inf for large x: |x·(1 + 1/f)|.
        cond = abs(a.value_ * (T(1) + T(1) / res));
    }
    T new_err  = cond * (a.rel_err_bound_ + u);
    T new_cond = std::max(a.max_cond_seen_, cond);
    TrackedId id = detail::make_id("expm1", loc);
    auto pv = a.prov_vars_;
    auto pc = a.prov_consts_;
    journal::emit("expm1", loc, id, {a.id_},
        (double)res, (double)cond, (double)new_err, pv, pc, flags);
    return Tracked<T>(res, new_err, new_cond, std::move(id), std::move(pv), std::move(pc));
}

// hypot(x,y): f = √(x² + y²), computed without intermediate overflow.
// Per-input κᵢ = xᵢ²/(x²+y²) ∈ [0,1]; under the max-gated error model the
// joint bound is κ = 1.  Never capped.  See docs/CONDITION_NUMBERS.md §3.10.
template <class T>
Tracked<T> hypot(const Tracked<T>& x, const Tracked<T>& y, SourceLocation loc = {}) {
    using std::hypot;
    T res      = hypot(x.value_, y.value_);
    T cond     = T(1);
    T max_in_err = std::max(x.rel_err_bound_, y.rel_err_bound_);
    T new_err    = cond * (max_in_err + unit_roundoff<T>());
    T new_cond   = std::max({x.max_cond_seen_, y.max_cond_seen_, cond});
    TrackedId id = detail::make_id("hypot", loc);
    auto pv      = detail::prov_union(x.prov_vars_,   y.prov_vars_);
    auto pc      = detail::prov_union(x.prov_consts_, y.prov_consts_);
    journal::emit("hypot", loc, id, {x.id_, y.id_},
        (double)res, (double)cond, (double)new_err, pv, pc);
    return Tracked<T>(res, new_err, new_cond, std::move(id), std::move(pv), std::move(pc));
}

// fma(a,b,c): f = a·b + c with a single rounding.  The conditioning is the
// add's: κ = (|a·b| + |c|)/|a·b + c| — cancellation between the product and
// c is fma's hotspot, and fma is the canonical fix for it (the product enters
// the cancellation EXACTLY, so no error is committed before the subtract).
// κ uses the rounded product (documented approximation, §3.11).  Exact zero
// ties are marked exact_tie; flush-to-zero underflow caps (cap="fma_uflow");
// NaN caps (cap="nan").
template <class T>
Tracked<T> fma(const Tracked<T>& a, const Tracked<T>& b, const Tracked<T>& c,
               SourceLocation loc = {}) {
    using std::fma; using std::abs;
    T res     = fma(a.value_, b.value_, c.value_);
    T p       = a.value_ * b.value_;
    T abs_res = abs(res);
    T abs_sum = abs(p) + abs(c.value_);
    T u       = unit_roundoff<T>();
    T cond;
    journal::EmitFlags flags;
    if (abs_res > T(0)) {
        cond = abs_sum / abs_res;
    } else if (std::isnan((double)res)) {
        cond = T(1) / u;
        flags.cap = "nan";
    } else if (abs_sum == T(0) || p == -c.value_) {
        cond = T(1);                       // exact zero tie: a·b == −c
        flags.exact_tie = true;
    } else {
        cond = T(1) / u;                   // res == 0 from unequal magnitudes
        flags.cap = "fma_uflow";
    }
    T max_in_err = std::max({a.rel_err_bound_, b.rel_err_bound_, c.rel_err_bound_});
    T new_err    = cond * (max_in_err + u);
    T new_cond   = std::max({a.max_cond_seen_, b.max_cond_seen_, c.max_cond_seen_, cond});
    TrackedId id = detail::make_id("fma", loc);
    auto pv = detail::prov_union(detail::prov_union(a.prov_vars_, b.prov_vars_),
                                 c.prov_vars_);
    auto pc = detail::prov_union(detail::prov_union(a.prov_consts_, b.prov_consts_),
                                 c.prov_consts_);
    journal::emit("fma", loc, id, {a.id_, b.id_, c.id_},
        (double)res, (double)cond, (double)new_err, pv, pc, flags);
    return Tracked<T>(res, new_err, new_cond, std::move(id), std::move(pv), std::move(pc));
}

} // namespace tracked
