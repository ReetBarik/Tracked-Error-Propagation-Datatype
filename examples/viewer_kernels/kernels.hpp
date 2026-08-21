#pragma once
// Example kernels for the viewer fixtures (docs/TRACKED_LIBRARY_PLAN.md,
// Phase 6). Each unit is a small, recognizable numerical computation whose
// journal exercises a specific part of the viewer:
//
//   cancellation        catastrophic (a+b)-a cancellation feeding a benign
//                       sink (local_cancellation region + cascade victim)
//   naive_variance      E[X^2] - E[X]^2 with var << mean^2 (elevated cond),
//                       two-function unit (mean_of called twice)
//   kahan               compensated summation: exact ties (exact_tie badge),
//                       intentional high-cond compensation; the first-order
//                       error bound explodes here (a documented model blind
//                       spot — max-gating cannot see the correlation that
//                       makes Kahan work), which stress-tests the viewer's
//                       extreme-value formatting
//   alternating_series  Taylor exp(-x) for large x: a cancellation *cascade*
//                       (no single op is extreme; the accumulated error is)
//   second_difference   (f2-f1)-(f1-f0) stencil: two severe first-difference
//                       cancellations on separate lines feeding one benign
//                       sink — a deterministic two-line cascade chain
//   log_sum_exp         naive log(exp a + exp b) + log(exp(a)) roundtrip
//                       (log-near-root, cap="log" at a==0) vs. a shifted
//                       rewrite on log1p that stays well-conditioned
//   polar_phase         hypot/atan2/sin/cos with per-sample cap branches
//                       (cap = "atan2"/"sin"/"cos") and genuine-cond samples
//   range_overflow      |val| outside float's range (range-guard badge),
//                       +Inf ("inf" sentinel), NaN from inf-inf (cap="nan")
//                       and 0/0 (plain non-finite val)
//   complex_logdiv      tracked::Complex log + Smith division near the unit
//                       circle, decomposed into real ops, plus an opaque
//                       external call
//
// Inputs are pure functions of the GLOBAL sample index (the chunkable-driver
// contract, docs/STREAMING.md): a journal slice rerun with --sample-offset i
// --sample-count 1 reproduces exactly the same values.
//
// The "line=" scopes mirror what tracked-line-inject emits around a hot
// statement; per the scope grammar (docs/SCHEMA.md) the value is a bare
// basename (a path would contain '/'). __LINE__+1 names the statement the
// scope wraps (the push sits on the line above it).

#include <tracked/tracked.hpp>
#include <tracked/ops.hpp>
#include <tracked/complex.hpp>

#include <string>

namespace viewer_kernels {

using T = tracked::Tracked<double>;

inline std::string line_scope(int line) {
    return "line=kernels.hpp:" + std::to_string(line);
}

// ---- cancellation ------------------------------------------------------------

inline T guarded_sum(T a, T b) {
    return tracked::add(a, b, TRACKED_HERE);
}

inline T cancellation_check(T a, T b) {
    auto s = guarded_sum(a, b);
    tracked::push_scope(line_scope(__LINE__ + 1));
    auto d = tracked::sub(s, a, TRACKED_HERE);   // catastrophic: d ~ b ~ 1e-15
    tracked::pop_scope();
    return tracked::div(d, s, TRACKED_HERE);     // benign sink, inherits rel_err
}

inline void run_cancellation(int s) {
    auto a = tracked::track("a", 1.0);
    auto b = tracked::track("b", 1.1e-15 * (1.0 + 0.03 * s));
    (void)cancellation_check(a, b);
}

// ---- naive_variance ----------------------------------------------------------

inline T mean_of(T sum, T n) {
    return tracked::div(sum, n, TRACKED_HERE);
}

inline T naive_variance(T sum_x, T sum_x2, T n) {
    auto mean  = mean_of(sum_x, n);
    auto ex2   = mean_of(sum_x2, n);
    auto mean2 = tracked::mul(mean, mean, TRACKED_HERE);
    tracked::push_scope(line_scope(__LINE__ + 1));
    auto var = tracked::sub(ex2, mean2, TRACKED_HERE);  // var << mean^2
    tracked::pop_scope();
    return var;
}

inline void run_naive_variance(int s) {
    const double n    = 1000.0;
    const double mean = 100.0 + 0.01 * s;
    const double var  = 1e-4 * (1.0 + 0.02 * s);
    auto sum_x  = tracked::track("sum_x",  n * mean);
    auto sum_x2 = tracked::track("sum_x2", n * (mean * mean + var));
    auto nn     = tracked::track("n", n);
    (void)naive_variance(sum_x, sum_x2, nn);
}

// ---- kahan -------------------------------------------------------------------

// One Kahan step; the compensation sub is *intentional* cancellation.  When a
// term is exactly representable in the running sum the compensation becomes an
// exact a-a tie (cond = 1, exact_tie marker) instead of a high-cond sub.
inline T kahan_step(T& sum, T& comp, T term) {
    auto y  = tracked::sub(term, comp, TRACKED_HERE);
    auto t  = tracked::add(sum, y, TRACKED_HERE);
    auto dt = tracked::sub(t, sum, TRACKED_HERE);
    comp    = tracked::sub(dt, y, TRACKED_HERE);
    sum     = t;
    return sum;
}

inline void run_kahan(int s) {
    auto sum  = tracked::track("acc", 1.0e8);
    auto comp = tracked::literal(0.0, TRACKED_HERE);
    for (int k = 1; k <= 6; ++k) {
        // odd k: exactly representable in 1e8 (exact ties); even k: not.
        const double v = (k % 2) ? double(k) : 0.1 * k * (1.0 + 0.001 * s);
        (void)kahan_step(sum, comp, tracked::literal(v, TRACKED_HERE));
    }
}

// ---- alternating_series --------------------------------------------------------

// Taylor series for exp(-x) at large x: every term is benign, no single op is
// catastrophic, but the partial sums swing to ~x^x/x! and collapse to e^-x, so
// the *accumulated* relative error is enormous — the canonical cancellation
// cascade.  The even/odd accumulation statements are separate source lines so
// the extracted cascade chain spans two regions.
inline T exp_taylor(T x, int terms) {
    auto sum  = tracked::literal(1.0, TRACKED_HERE);
    auto term = tracked::literal(1.0, TRACKED_HERE);
    auto negx = tracked::neg(x, TRACKED_HERE);
    for (int k = 1; k <= terms; ++k) {
        auto ratio = tracked::div(negx, tracked::literal(double(k), TRACKED_HERE),
                                  TRACKED_HERE);
        term = tracked::mul(term, ratio, TRACKED_HERE);
        if (k % 2) {
            sum = tracked::add(sum, term, TRACKED_HERE);
        } else {
            sum = tracked::add(sum, term, TRACKED_HERE);
        }
    }
    return sum;
}

inline void run_alternating_series(int s) {
    auto x = tracked::track("x", 12.5 + 0.08 * s);
    (void)exp_taylor(x, 36);
}

// ---- second_difference ---------------------------------------------------------

// Central second difference (f2 - f1) - (f1 - f0) on a smooth, slowly-varying
// f: both first differences on their own lines are severe cancellations, and
// the final sub is benign (the differences differ by ~2x) — a deterministic
// two-line cascade chain feeding one victim.
inline T second_difference(T f0, T f1, T f2) {
    auto d1 = tracked::sub(f2, f1, TRACKED_HERE);
    auto d2 = tracked::sub(f1, f0, TRACKED_HERE);
    return tracked::sub(d1, d2, TRACKED_HERE);
}

inline void run_second_difference(int s) {
    const double h = 3e-11 * (1.0 + 0.01 * s);
    auto f0 = tracked::track("f0", 1.0);
    auto f1 = tracked::track("f1", 1.0 + h);
    auto f2 = tracked::track("f2", 1.0 + 3.0 * h);
    (void)second_difference(f0, f1, f2);
}

// ---- log_sum_exp ---------------------------------------------------------------

inline T lse_naive(T a, T b) {
    auto ea = tracked::exp(a, TRACKED_HERE);
    auto eb = tracked::exp(b, TRACKED_HERE);
    auto se = tracked::add(ea, eb, TRACKED_HERE);
    return tracked::log(se, TRACKED_HERE);
}

// log(exp(a)) — log near its root when a ~ 0; exactly at a == 0 the library
// saturates the log cond (cap="log").
inline T exp_log_roundtrip(T a) {
    auto ea = tracked::exp(a, TRACKED_HERE);
    return tracked::log(ea, TRACKED_HERE);
}

// The stable rewrite (assumes a >= b): a + log1p(exp(b - a)).  log1p is
// well-conditioned exactly where log(1 + eps) is not — the Phase-4 rationale.
inline T lse_shifted(T a, T b) {
    auto d  = tracked::sub(b, a, TRACKED_HERE);
    auto ed = tracked::exp(d, TRACKED_HERE);
    auto l1 = tracked::log1p(ed, TRACKED_HERE);
    return tracked::add(a, l1, TRACKED_HERE);
}

inline void run_log_sum_exp(int s) {
    const double av = (s % 4 == 0) ? 0.0 : -1e-7 * (s + 1);
    auto a = tracked::track("a", av);
    auto b = tracked::track("b", -3.0 - 0.37 * s);
    (void)lse_naive(a, b);
    (void)exp_log_roundtrip(a);
    (void)lse_shifted(a, b);
}

// ---- polar_phase ---------------------------------------------------------------

inline T phase_angle(T y, T x) {
    auto r  = tracked::hypot(x, y, TRACKED_HERE);
    auto th = tracked::atan2(y, x, TRACKED_HERE);
    return tracked::mul(th, r, TRACKED_HERE);
}

inline T resolve(T th) {
    auto sn  = tracked::sin(th, TRACKED_HERE);
    auto cs  = tracked::cos(th, TRACKED_HERE);
    auto sn2 = tracked::mul(sn, sn, TRACKED_HERE);
    auto cs2 = tracked::mul(cs, cs, TRACKED_HERE);
    return tracked::add(sn2, cs2, TRACKED_HERE);
}

inline void run_polar_phase(int s) {
    auto x = tracked::track("x", 1.0 + 0.01 * s);
    // even samples: y = 0 -> atan2 result 0 -> cap="atan2"
    auto y = tracked::track("y", (s % 2 == 0) ? 0.0 : 0.02 * s);
    (void)phase_angle(y, x);
    // even samples: theta = pi (sin cap) resp. pi/2 via the odd branch below
    const double tv = (s % 2 == 0) ? 3.141592653589793      // sin(pi) ~ 1e-16
                                   : 1.5707963267948966;    // cos(pi/2) ~ 6e-17
    auto th = tracked::track("theta", tv);
    (void)resolve(th);
}

// ---- range_overflow ------------------------------------------------------------

inline void run_range_overflow(int s) {
    auto h  = tracked::track("h", 1e60 * (1.0 + 0.01 * s));
    auto hh = tracked::mul(h, h, TRACKED_HERE);      // 1e120 > FLT_MAX
    auto t  = tracked::track("t", 1e-50);
    auto tt = tracked::mul(t, t, TRACKED_HERE);      // 1e-100 < FLT_MIN_NORMAL
    auto h4 = tracked::mul(hh, hh, TRACKED_HERE);    // 1e240
    auto ov = tracked::mul(h4, h4, TRACKED_HERE);    // overflow: val = "inf"
    auto nn = tracked::sub(ov, ov, TRACKED_HERE);    // inf - inf: cap="nan"
    (void)tt; (void)nn;
    auto zn = tracked::track("zn", 0.0);
    auto dz = tracked::div(zn, zn, TRACKED_HERE);    // 0/0: val = "nan"
    (void)dz;
}

// ---- complex_logdiv ------------------------------------------------------------

inline void run_complex_logdiv(int s) {
    // |z| ~ 1, so log|z| sits near the log root (elevated cond); every complex
    // op decomposes into journaled real ops sharing this call site's location.
    auto z  = tracked::track("z", 0.8, 0.6 + 1e-9 * (s + 1));
    auto w  = tracked::track("w", 1.0 + 1e-6 * (s + 1), -0.5);
    auto lz = tracked::log(z, TRACKED_HERE);
    auto d  = z / w;                                 // Smith division
    auto o  = tracked::opaque_at("ext::blas_scale", d.real().value() * 2.0,
                                 TRACKED_HERE, d.real());
    (void)lz; (void)o;
}

// ---- unit table ----------------------------------------------------------------

struct Unit {
    const char* name;
    void (*fn)(int global_sample_index);
};

inline constexpr Unit kUnits[] = {
    {"cancellation",       run_cancellation},
    {"naive_variance",     run_naive_variance},
    {"kahan",              run_kahan},
    {"alternating_series", run_alternating_series},
    {"second_difference",  run_second_difference},
    {"log_sum_exp",        run_log_sum_exp},
    {"polar_phase",        run_polar_phase},
    {"range_overflow",     run_range_overflow},
    {"complex_logdiv",     run_complex_logdiv},
};

} // namespace viewer_kernels
