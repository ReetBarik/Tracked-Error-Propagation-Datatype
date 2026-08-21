// Phase-4 op coverage: log1p, expm1, hypot, fma (docs/CONDITION_NUMBERS.md
// §3.8–3.11). Each is the canonical *fix* for a signal the reducer hunts, so
// the calibration asserts both directions: the naive spelling is pathological
// AND the new op is well-conditioned on the same inputs — the conditioning
// improvement a rewrite must be able to demonstrate.

#include <catch2/catch_test_macros.hpp>
#include <catch2/catch_approx.hpp>

#include <tracked/tracked.hpp>
#include <tracked/ops.hpp>

#include <cmath>
#include <limits>

using tracked::track;
using tracked::journal::clear;
using tracked::journal::records;
using Catch::Approx;

TEST_CASE("log1p is the conditioning fix for log near 1") {
    clear();
    auto x   = track("x", 1e-8);
    auto one = track("one", 1.0);

    auto naive = tracked::log(one + x, TRACKED_HERE);   // log(1+x): cond ~ 1e8
    double naive_cond = records().back().cond;
    REQUIRE(naive_cond > 1e7);

    auto fixed = tracked::log1p(x, TRACKED_HERE);       // same value, cond ~ 1
    double fixed_cond = records().back().cond;
    REQUIRE(fixed_cond < 2.0);
    REQUIRE(fixed.value() == Approx(naive.value()).epsilon(1e-6));
}

TEST_CASE("log1p limits, cap, and NaN cause") {
    clear();
    auto z = track("z", 0.0);
    auto r0 = tracked::log1p(z, TRACKED_HERE);          // exact 0, kappa -> 1
    REQUIRE(r0.value() == 0.0);
    REQUIRE(records().back().cond == 1.0);
    REQUIRE(records().back().cap.empty());

    auto m1 = track("m1", -1.0);                        // the log singularity
    tracked::log1p(m1, TRACKED_HERE);
    REQUIRE(records().back().cap == "log1p");
    REQUIRE(records().back().cond == 1.0 / tracked::unit_roundoff<double>());

    auto m2 = track("m2", -2.0);                        // domain error -> NaN
    tracked::log1p(m2, TRACKED_HERE);
    REQUIRE(records().back().cap == "nan");
}

TEST_CASE("expm1 is the conditioning fix for exp(x)-1 cancellation") {
    clear();
    auto x   = track("x", 1e-8);
    auto one = track("one", 1.0);

    auto e     = tracked::exp(x, TRACKED_HERE);
    auto naive = e - one;                               // sub cond ~ 2e8
    double naive_cond = records().back().cond;
    REQUIRE(naive_cond > 1e7);

    auto fixed = tracked::expm1(x, TRACKED_HERE);       // same value, cond ~ 1
    double fixed_cond = records().back().cond;
    REQUIRE(fixed_cond < 2.0);
    REQUIRE(fixed.value() == Approx(naive.value()).epsilon(1e-6));
}

TEST_CASE("expm1 limits: kappa ~ |x| for large x, ~ 0 for very negative x") {
    clear();
    auto big = track("big", 40.0);
    tracked::expm1(big, TRACKED_HERE);
    REQUIRE(records().back().cond == Approx(40.0).epsilon(1e-6));
    REQUIRE(records().back().cap.empty());              // finite: never capped

    auto neg = track("neg", -40.0);
    tracked::expm1(neg, TRACKED_HERE);
    REQUIRE(records().back().cond < 1e-10);

    auto z = track("z", 0.0);
    tracked::expm1(z, TRACKED_HERE);
    REQUIRE(records().back().cond == 1.0);
}

TEST_CASE("hypot: cond 1, and no intermediate overflow") {
    clear();
    auto a = track("a", 3.0);
    auto b = track("b", 4.0);
    auto r = tracked::hypot(a, b, TRACKED_HERE);
    REQUIRE(r.value() == 5.0);
    REQUIRE(records().back().cond == 1.0);
    REQUIRE(records().back().in.size() == 2);

    // the range-guard fix: sqrt(x^2 + y^2) would overflow at 3e300
    auto hx = track("hx", 3e300);
    auto hy = track("hy", 4e300);
    auto hr = tracked::hypot(hx, hy, TRACKED_HERE);
    REQUIRE(std::isfinite(hr.value()));
    REQUIRE(hr.value() == Approx(5e300));

    // provenance merges both inputs
    REQUIRE(hr.prov_vars().count("hx") == 1);
    REQUIRE(hr.prov_vars().count("hy") == 1);
}

TEST_CASE("fma: add-shaped conditioning with the exact-product residual") {
    clear();
    // Residual extraction: x = 1 + 2^-27, x^2 rounds away its last bit; fma
    // recovers it exactly. The measured cond is the genuine catastrophic-
    // cancellation number (~2^55) — visible, not hidden by a pre-rounded mul.
    double xv = 1.0 + std::ldexp(1.0, -27);
    double p  = xv * xv;                                // rounded product
    auto x = track("x", xv);
    auto c = track("c", -p);
    auto r = tracked::fma(x, x, c, TRACKED_HERE);
    REQUIRE(r.value() == std::ldexp(1.0, -54));         // exact residual
    REQUIRE(records().back().cond > 1e16);
    REQUIRE(records().back().cap.empty());
    REQUIRE(records().back().in.size() == 3);

    // benign: no cancellation
    auto a2 = track("a2", 2.0);
    auto b2 = track("b2", 3.0);
    auto c2 = track("c2", 4.0);
    tracked::fma(a2, b2, c2, TRACKED_HERE);
    REQUIRE(records().back().cond == 1.0);
    REQUIRE_FALSE(records().back().exact_tie);
}

TEST_CASE("fma exact tie and NaN cap") {
    clear();
    auto a = track("a", 2.0);
    auto b = track("b", 3.0);
    auto c = track("c", -6.0);
    auto r = tracked::fma(a, b, c, TRACKED_HERE);       // 2*3 + (-6) == 0
    REQUIRE(r.value() == 0.0);
    REQUIRE(records().back().exact_tie);
    REQUIRE(records().back().cond == 1.0);

    auto inf  = track("inf", std::numeric_limits<double>::infinity());
    auto ninf = track("ninf", -std::numeric_limits<double>::infinity());
    auto one  = track("one", 1.0);
    tracked::fma(inf, one, ninf, TRACKED_HERE);         // inf - inf -> NaN
    REQUIRE(records().back().cap == "nan");
    REQUIRE_FALSE(records().back().exact_tie);
}
