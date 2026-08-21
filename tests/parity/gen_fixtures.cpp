// Parity-fixture driver: generates deterministic journals over small example
// kernels for the tracked-reduce differential-parity harness.
//
// IMPORTANT — schema freeze: the committed fixtures under
// tests/parity/fixtures/ were generated with the PRE-v1 library (headerless
// v0.3/v0.4 record format) and are the frozen legacy-mode corpus. Regenerating
// them with a post-schema-break library produces v1 journals; do that only to
// create NEW v1 fixtures, never to overwrite the v0.3 set.
//
// Conventions mirrored from the AMP consumer's sharded characterizer:
//   * every (kernel, sample) runs under nested scopes
//     "integral=<kernel>" / "sample=<global index>", so ids carry the suffix
//     "@integral=<k>/sample=<i>" and the reducer can group per-sample batches;
//   * some statements are wrapped in an injected-style "line=<file>:<N>" scope
//     (what tracked-line-inject emits);
//   * a mix of named ops (TRACKED_HERE -> "at" populated) and operator forms
//     ("at" empty, anonymous "<op>@?#n" ids).
//
// Inputs are pure functions of the GLOBAL sample index (no RNG state), so a
// journal sliced by --sample-offset/--sample-count is bit-identical to the
// same range inside a longer run (the chunkable-driver prefix-fill contract).
//
// Signals deliberately exercised (so parity on these fixtures is not vacuous):
//   cancellation      catastrophic sub (local_cancellation region) feeding a
//                     benign sink (cascade victim via leaf-operand fallback)
//   variance_cascade  near-cancelling sub of two INTERNAL nodes (val-based
//                     cancellation ratio path) feeding a benign sink
//   kahan             compensated summation: exact ties (cond=1), operator-form
//                     anonymous ids, empty region key
//   trig_gate         saturation caps (cond = 2^53) from three distinct causes:
//                     atan2(0,1), log(1.0), sin(pi) — indistinguishable in the
//                     v0.3 schema (the motivation for the v1 "cap" field)
//   logsumexp         exp/log chains, log near a root (elevated cond ~1e7),
//                     rel_err spread across decades for the LogHist
//   range_overflow    |val| outside float's range (guard), +Inf clamped to
//                     DBL_MAX in JSON, NaN -> null
//   complex_logdiv    tracked::Complex log + divide decomposed into real ops
//                     (norm/sqrt/atan2/div...) near the unit circle

#include <tracked/tracked.hpp>
#include <tracked/ops.hpp>
#include <tracked/complex.hpp>
#include <tracked/journal.hpp>

#include <cstdio>
#include <cstdlib>
#include <string>

using tracked::Tracked;
using tracked::track;
using tracked::constant;
using tracked::literal;

namespace {

// ---- kernels ----------------------------------------------------------------

void k_cancellation(int s) {
    auto a = track("a", 1.0 + (s + 1) * 3e-12);
    auto b = track("b", 1.0);
    auto t = add(a, b, TRACKED_HERE);
    tracked::push_scope("line=cancellation_kernel.hpp:12");
    auto d = sub(a, b, TRACKED_HERE);          // catastrophic cancellation
    tracked::pop_scope();
    auto q = div(d, t, TRACKED_HERE);          // benign sink -> cascade victim
    (void)q;
}

void k_variance_cascade(int s) {
    auto x = track("x", 100.0 + 7e-9 * (1.0 + 0.01 * s));
    auto y = track("y", 100.0 - 3e-9 * (1.0 + 0.01 * s));
    auto c = constant("cal", 1.0000001);
    auto p = mul(x, c, TRACKED_HERE);          // internal node
    auto q = mul(y, c, TRACKED_HERE);          // internal node, p ~ q
    tracked::push_scope("line=variance_kernel.hpp:31");
    auto d = sub(p, q, TRACKED_HERE);          // val-based cancellation ratio
    tracked::pop_scope();
    auto e = mul(d, literal(0.5, TRACKED_HERE), TRACKED_HERE);
    auto r = add(d, e, TRACKED_HERE);          // benign sink -> cascade victim
    (void)r;
}

void k_kahan(int s) {
    auto sum = track("acc", 0.0);
    auto comp = literal(0.0, TRACKED_HERE);
    for (int k = 1; k <= 5; ++k) {
        auto term = div(constant("one", 1.0),
                        literal(double(k + s % 3), TRACKED_HERE), TRACKED_HERE);
        auto y  = term - comp;                 // operator form: at="", anon id
        auto t  = sum + y;
        auto dt = t - sum;                     // near/exact ties
        comp    = dt - y;                      // exact tie -> cond = 1
        sum     = t;
    }
}

void k_trig_gate(int s) {
    auto one = track("one", 1.0);
    if (s % 2 == 0) {
        auto z = track("z", 0.0);
        auto g = tracked::atan2(z, one, TRACKED_HERE);   // cap: cond = 2^53
        (void)g;
        auto w = track("w", 1.0);
        auto lg = tracked::log(w, TRACKED_HERE);         // cap: cond = 2^53
        (void)lg;
    } else {
        auto z = track("z", 1e-3 * s);
        auto g = tracked::atan2(z, one, TRACKED_HERE);   // genuine finite cond
        (void)g;
        auto w = track("w", 1.0 + 1e-4 * s);
        auto lg = tracked::log(w, TRACKED_HERE);         // elevated finite cond
        (void)lg;
    }
    auto p  = track("p", 3.141592653589793);
    auto sn = tracked::sin(p, TRACKED_HERE);             // cap: cond = 2^53
    (void)sn;
}

void k_logsumexp(int s) {
    auto x1 = track("x1", -1e-7 * (s + 1));
    auto e1 = tracked::exp(x1, TRACKED_HERE);            // e1 ~ 1 - 1e-7(s+1)
    auto l1 = tracked::log(e1, TRACKED_HERE);            // log near root: cond ~ 1e7/(s+1)
    auto x2 = track("x2", -2.0 - 0.37 * s);
    auto e2 = tracked::exp(x2, TRACKED_HERE);
    auto se = add(e1, e2, TRACKED_HERE);
    auto ls = tracked::log(se, TRACKED_HERE);
    auto r  = add(l1, ls, TRACKED_HERE);
    (void)r;
}

void k_range_overflow(int s) {
    auto h  = track("h", 1e60 * (1.0 + s / 100.0));
    auto hh = mul(h, h, TRACKED_HERE);                   // 1e120 > FLT_MAX
    auto t  = track("t", 1e-50);
    auto tt = mul(t, t, TRACKED_HERE);                   // 1e-100 < FLT_MIN
    auto h4 = mul(hh, hh, TRACKED_HERE);                 // 1e240
    auto ov = mul(h4, h4, TRACKED_HERE);                 // +Inf -> clamped DBL_MAX
    (void)ov; (void)tt;
    auto zn = track("zn", 0.0);
    auto nn = div(zn, zn, TRACKED_HERE);                 // NaN -> null val
    (void)nn;
}

void k_cascade_range(int s) {
    // A cascade chain whose contributor line ALSO violates float's range, so
    // the finalized chain record's value_range_ok_for_float is False — pinning
    // chain_range_ok's non-fail-open path (all other kernels' chains are
    // range-safe, which would leave that branch untested).
    auto x = track("rx", 1e-40 * (1.0 + 7e-11 * (1.0 + 0.01 * s)));
    auto y = track("ry", 1e-40 * (1.0 - 3e-11 * (1.0 + 0.01 * s)));
    auto c = constant("rc", 1.0000001);
    auto p = mul(x, c, TRACKED_HERE);          // ~1e-40 < FLT_MIN_NORMAL
    auto q = mul(y, c, TRACKED_HERE);
    tracked::push_scope("line=cascade_range_kernel.hpp:7");
    auto d = sub(p, q, TRACKED_HERE);          // ~1e-50: contributor line is
    tracked::pop_scope();                      // float-range-unsafe
    auto e = mul(d, literal(0.5, TRACKED_HERE), TRACKED_HERE);
    auto r = add(d, e, TRACKED_HERE);          // benign sink -> cascade victim
    (void)r;
}

void k_complex_logdiv(int s) {
    auto z = tracked::track("z", 0.8, 0.6 + 1e-9 * (s + 1));   // |z| ~ 1
    auto w = tracked::track("w", 1.0 + 1e-6 * (s + 1), -0.5);
    auto lz = tracked::log(z, TRACKED_HERE);             // log|z| near root
    auto d  = z / w;                                     // Smith division ops
    auto o  = tracked::opaque_at("ext::blas_scale", d.real().value() * 2.0,
                                 TRACKED_HERE, d.real());
    (void)lz; (void)o;
}

struct Kernel {
    const char* name;
    void (*fn)(int);
};

const Kernel kKernels[] = {
    {"cancellation",     k_cancellation},
    {"variance_cascade", k_variance_cascade},
    {"kahan",            k_kahan},
    {"trig_gate",        k_trig_gate},
    {"logsumexp",        k_logsumexp},
    {"range_overflow",   k_range_overflow},
    {"cascade_range",    k_cascade_range},
    {"complex_logdiv",   k_complex_logdiv},
};

} // namespace

int main(int argc, char** argv) {
    if (argc != 4) {
        std::fprintf(stderr,
            "usage: %s <out.jsonl> <sample_offset> <sample_count>\n", argv[0]);
        return 2;
    }
    const std::string out = argv[1];
    const int offset = std::atoi(argv[2]);
    const int count  = std::atoi(argv[3]);

    for (const auto& k : kKernels) {
        tracked::scope integral_scope(std::string("integral=") + k.name);
        for (int i = offset; i < offset + count; ++i) {
            tracked::scope sample_scope("sample=" + std::to_string(i));
            k.fn(i);
        }
    }
    tracked::journal::flush(out);
    std::printf("%s: %zu records\n", out.c_str(),
                tracked::journal::records().size());
    return 0;
}
