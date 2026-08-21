#include <catch2/catch_test_macros.hpp>
#include <catch2/catch_approx.hpp>

#include <tracked/tracked.hpp>

#include <cmath>
#if defined(__x86_64__) || defined(__i386__)
#include <xmmintrin.h>   // FTZ control, to force a genuine underflow-to-zero
#endif

using Catch::Approx;
using tracked::track;

// Calibration: catastrophic cancellation detection.
//
// Cancellation occurs in (a - b) when a ≈ b: large operands, tiny result.
// Condition number = (|a|+|b|)/|a-b| >> 1 amplifies any input error.
// Tracked<T> flags this automatically.

TEST_CASE("cancellation: (1 + eps) - 1 has cond >> 1") {
    // Exact result = eps.  FP keeps the correct value here (eps is representable),
    // but all relative accuracy in the result came from cancellation.
    // cond = (|1+eps| + |1|) / |eps| ≈ 2/eps = 2e10 >> 1e8.
    double eps = 1e-10;
    auto one   = track("one", 1.0);
    auto eps_t = track("eps", eps);
    auto sum   = one + eps_t;   // cond ≈ 1, no cancellation
    auto diff  = sum - one;     // cond ≈ 2e10 — catastrophic

    REQUIRE(diff.max_cond_seen_ > 1e8);
}

TEST_CASE("cancellation: rel_err_bound grows by factor of cond") {
    // After a cancelling subtraction with cond ≈ 2e10, the relative error
    // bound on the result is cond × 2u ≈ 2e10 × 2.2e-16 ≈ 4.4e-6.
    // That means ~10 decimal digits have been lost.
    double eps = 1e-10;
    auto a = track("a", 1.0 + eps);
    auto b = track("b", 1.0);
    auto r = a - b;

    double u = tracked::unit_roundoff<double>();
    REQUIRE(r.rel_err_bound_ > 1e4 * u);   // at least 10 000× fresh unit_roundoff
}

TEST_CASE("cancellation: two cancellations in sequence compound the error") {
    // r1 = (1 + eps) - 1  →  cond ≈ 2e10
    // r2 = (r1 + r1) - r1  →  same cond again; max_cond_seen carries forward
    double eps = 1e-9;
    auto a = track("a", 1.0 + eps);
    auto b = track("b", 1.0);
    auto r1 = a - b;
    auto r2 = r1 + r1;
    auto r3 = r2 - r1;   // another cancelling subtraction on already-damaged values

    // max_cond_seen accumulates across the chain
    REQUIRE(r3.max_cond_seen_ > 1e8);
    // rel_err_bound has grown dramatically from fresh u ≈ 1.1e-16
    REQUIRE(r3.rel_err_bound_ > 1e-8);
}

// ---- Exact cancellation: cond == 1, not the 1/u catastrophe sentinel --------
//
// When a - b (or a + (-b)) cancels *exactly*, the result is exactly 0 and no
// precision is lost — this is deterministic cancellation of identical values,
// not near-cancellation. Previously these reported cond = 1/u ≈ 9e15, polluting
// hotspot tails with spurious "no significant digits left" records.

TEST_CASE("sub: exact cancellation of identical values is cond == 1") {
    // Distinct provenance ids, identical value: x - x2 with both = 3.14.
    auto x  = track("x",  3.14);
    auto x2 = track("x2", 3.14);
    auto r  = x - x2;

    double u = tracked::unit_roundoff<double>();
    REQUIRE(r.value_ == 0.0);
    REQUIRE(r.max_cond_seen_ == Approx(1.0));
    // rel_err = cond * (max_in_err + u) = 1 * (u + u) = 2u. Bounded by ~2u.
    REQUIRE(r.rel_err_bound_ == Approx(2.0 * u));
}

TEST_CASE("sub: exact cancellation of zeros is cond == 1 (regression)") {
    // Previously handled by the abs_sum == 0 branch; now by the a == b branch.
    auto z1 = track("z1", 0.0);
    auto z2 = track("z2", 0.0);
    auto r  = z1 - z2;

    REQUIRE(r.value_ == 0.0);
    REQUIRE(r.max_cond_seen_ == Approx(1.0));
}

TEST_CASE("sub: near-cancellation (one ULP apart) still reports cond ~ 1/u") {
    // Guards against over-broadening the fix: values that differ by a single
    // ULP are NOT equal, so genuine cancellation must still surface.
    double av = 1.0;
    double bv = std::nextafter(1.0, 2.0);   // 1 ULP above 1.0
    auto a = track("a", av);
    auto b = track("b", bv);
    auto r = a - b;

    REQUIRE(r.value_ != 0.0);
    REQUIRE(r.max_cond_seen_ > 1e15);        // ~ 1/u ≈ 9e15
}

TEST_CASE("add: exact negation x + (-x) for nonzero x is cond == 1") {
    auto x    = track("x",    2.5);
    auto negx = track("negx", -2.5);
    auto r    = x + negx;

    REQUIRE(r.value_ == 0.0);
    REQUIRE(r.max_cond_seen_ == Approx(1.0));
}

TEST_CASE("add: 0 + (-0) is cond == 1 (the a != 0 guard)") {
    // IEEE has 0 == -0; the != 0 guard must keep this out of the exact-negation
    // branch and let it resolve to cond = 1 via the both-zero case, NOT 1/u.
    auto z  = track("z",  0.0);
    auto nz = track("nz", -0.0);
    auto r  = z + nz;

    REQUIRE(r.value_ == 0.0);
    REQUIRE(r.max_cond_seen_ == Approx(1.0));
}

TEST_CASE("sub: genuine subnormal underflow to 0 (a != b) reports cond ~ 1/u") {
    // Proves the abs_res == 0 fallback survives the exact-equality shortcut.
    //
    // Under IEEE round-to-nearest with gradual underflow, fl(a - b) == 0 for
    // finite doubles happens ONLY when a == b — a distinct pair cannot subtract
    // to exactly zero. To exercise the fallback with unequal inputs we must
    // enable hardware flush-to-zero so a subnormal difference collapses to 0.
    double u = tracked::unit_roundoff<double>();

#if defined(__x86_64__) || defined(__i386__)
    unsigned old_mode = _MM_GET_FLUSH_ZERO_MODE();
    _MM_SET_FLUSH_ZERO_MODE(_MM_FLUSH_ZERO_ON);
    double av = 0x1.8p-1022;   // 1.5 * 2^-1022  (normal)
    double bv = 0x1.0p-1022;   // 2^-1022, smallest normal
    auto a = track("a", av);
    auto b = track("b", bv);
    auto r = a - b;            // exact diff 2^-1023 (subnormal) -> FTZ -> 0.0
    _MM_SET_FLUSH_ZERO_MODE(old_mode);

    REQUIRE(av != bv);
    REQUIRE(r.value_ == 0.0);
    REQUIRE(r.max_cond_seen_ == Approx(1.0 / u));
#else
    // Portable fallback: no reliable FTZ control. Exercise the adjacent regime
    // — two distinct near-equal subnormals whose difference stays nonzero must
    // still report a large cond via the normal branch, not be swallowed as
    // exact cancellation. av = 2^-1023 + 2^-1074, bv = 2^-1023: both exactly
    // representable subnormals, diff = 2^-1074, cond = 2^52 + 1 ~ 4.5e15.
    double av = 0x1.0000000000002p-1023;
    double bv = 0x1.0p-1023;
    auto a = track("a", av);
    auto b = track("b", bv);
    auto r = a - b;

    REQUIRE(r.value_ != 0.0);
    REQUIRE(r.max_cond_seen_ > 1e15);
#endif
}
