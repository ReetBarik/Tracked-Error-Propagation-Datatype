#pragma once
// Tracked<T> — floating-point wrapper that propagates condition numbers,
// accumulated relative error bounds, and provenance alongside the value,
// and emits a structured JSONL log of every op.
//
// v0.3: every value carries a stable TrackedId. Source variables and named
// constants are two distinct categories (track() vs constant()); derived values
// get a generated id "<op>@<file>:<line>#<callsite_counter>[@<scope>]".
// See docs/PROVENANCE.md.
//
// v0.4: literal() completes the factory taxonomy — an anonymous scalar promoted
// into the graph with an auto-generated "_lit@..." id but no provenance role.
// See docs/PLAN-v0.4.md.
//
// Usage:
//   #include <tracked/tracked.hpp>
//   #include <tracked/ops.hpp>     // for sqrt, exp, log, abs
//
//   auto a = tracked::track("a", 1.0);       // source variable  -> prov_vars
//   auto k = tracked::constant("two", 2.0);  // named constant    -> prov_consts
//   auto r = a - k;                          // operator, no source location
//   auto r = sub(a, k, TRACKED_HERE);        // named fn, captures file:func:line
//
//   tracked::journal::flush("out.jsonl");

#include <tracked/journal.hpp>
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <set>
#include <string>
#include <string_view>
#include <vector>

namespace tracked {

// A stable identity for a tracked value: a source/constant name, or a generated
// id for a derived value.
using TrackedId = std::string;

// ---- Numeric constants ------------------------------------------------------

template <class T>
constexpr T unit_roundoff() {
    return std::numeric_limits<T>::epsilon() / T(2);
}

// ---- Internal helpers -------------------------------------------------------

namespace detail {

inline std::set<std::string> prov_union(const std::set<std::string>& a,
                                        const std::set<std::string>& b) {
    std::set<std::string> r = a;
    r.insert(b.begin(), b.end());
    return r;
}

// ---- Value-id generation ----------------------------------------------------
//
// The callsite counter lives in tracked::journal::detail so journal::clear()
// can reset it alongside the record buffer.  The scope stack is lexical (owned
// by the RAII scope class) and deliberately NOT reset by clear().

inline thread_local std::vector<std::string> scope_stack;

// Join the scope stack with '/', prefixed with '@'.  Empty when no scope.
inline std::string current_scope_suffix() {
    if (scope_stack.empty()) return "";
    std::string s = "@";
    for (std::size_t i = 0; i < scope_stack.size(); ++i) {
        if (i) s += '/';
        s += scope_stack[i];
    }
    return s;
}

// Build a stable id for a derived value produced by `op` at `loc`.
//   with location:    "<op>@<file>:<line>#<n>[@<scope>]"
//   without location: "<op>@?#<n>[@<scope>]"   (operator-form calls, loc.line==0)
// The counter is keyed on (file, line, op) and is thread-local; run-to-run it
// reproduces the same ids for the same call sequence (see PROVENANCE.md).
inline TrackedId make_id(std::string_view op, const SourceLocation& loc) {
    if (loc.line == 0) {
        std::uint64_t n = ++journal::detail::anon_counter;
        return std::string(op) + "@?#" + std::to_string(n) + current_scope_suffix();
    }
    journal::detail::CallsiteKey k{loc.file, loc.line, op};
    std::uint64_t n = ++journal::detail::callsite_counters[k];
    return std::string(op) + '@' + std::string(loc.file) + ':' + std::to_string(loc.line)
         + '#' + std::to_string(n) + current_scope_suffix();
}

} // namespace detail

// ---- RAII scope helper ------------------------------------------------------
//
// Pushes a context string onto a thread-local stack; the joined stack is
// appended to every generated id as "@<scope>".  Nested scopes join with '/':
//   tracked::scope run("run=A");
//   { tracked::scope s("s=1"); ... }   // ids get "@run=A/s=1"
class scope {
public:
    explicit scope(std::string ctx) { detail::scope_stack.push_back(std::move(ctx)); }
    ~scope() { detail::scope_stack.pop_back(); }
    scope(const scope&)            = delete;
    scope& operator=(const scope&) = delete;
};

// ---- Free scope push/pop ----------------------------------------------------
//
// The RAII `scope` class scopes a *lexical block*.  Some instrumentation must
// scope a single statement that includes a declaration — brace-wrapping such a
// statement (`{ scope s(...); T x = ...; }`) would scope `x` out of the enclosing
// block.  These free functions push/pop the same thread-local stack without a
// block, so a declaration statement can be wrapped as
// `push_scope("line=f.h:10"); T x = ...; pop_scope();` with `x` still in scope.
// Same LIFO contract as `scope`: every push must be balanced by exactly one pop.
inline void push_scope(std::string ctx) {
    detail::scope_stack.push_back(std::move(ctx));
}
inline void pop_scope() {
    detail::scope_stack.pop_back();
}

// ---- Forward declarations (definitions after the class) ---------------------

template <class T> class Tracked;
template <class T> Tracked<T> add(const Tracked<T>&, const Tracked<T>&, SourceLocation loc = {});
template <class T> Tracked<T> sub(const Tracked<T>&, const Tracked<T>&, SourceLocation loc = {});
template <class T> Tracked<T> mul(const Tracked<T>&, const Tracked<T>&, SourceLocation loc = {});
template <class T> Tracked<T> div(const Tracked<T>&, const Tracked<T>&, SourceLocation loc = {});
template <class T> Tracked<T> neg(const Tracked<T>&, SourceLocation loc = {});

template <class T, class... Inputs>
Tracked<T> opaque_at(std::string_view fn_name, T value, SourceLocation loc,
                     const Inputs&... inputs);

// ---- Tracked<T> -------------------------------------------------------------

template <class T>
class Tracked {
public:
    T                     value_;
    T                     rel_err_bound_;  // accumulated relative error bound
    T                     max_cond_seen_;  // max local condition on any op producing this
    TrackedId             id_;             // stable identity of this value
    std::set<std::string> prov_vars_;      // contributing source-variable ids
    std::set<std::string> prov_consts_;    // contributing named-constant ids

    Tracked() : value_(T(0)), rel_err_bound_(T(0)), max_cond_seen_(T(0)) {}

    // Unnamed literal (e.g. Tracked<T>(0.5) inside a kernel). No provenance, but
    // it synthesizes a real literal id ("_lit@?#<n>", or with a source location if
    // one is threaded through literal()) so a value promoted through this ctor can
    // never enter an op with an empty operand id. Prefer track()/constant() for
    // anything you want attributed; prefer literal(v, TRACKED_HERE) to capture a
    // source location. The id is synthesized inline via detail::make_id rather than
    // by delegating to literal(), because literal() constructs through this very
    // ctor and delegation would recurse.
    explicit Tracked(T v)
        : value_(v), rel_err_bound_(unit_roundoff<T>()), max_cond_seen_(T(0))
        , id_(detail::make_id("_lit", SourceLocation{})) {}

    Tracked(T v, T err, T cond, TrackedId id,
            std::set<std::string> pv, std::set<std::string> pc)
        : value_(v), rel_err_bound_(err), max_cond_seen_(cond)
        , id_(std::move(id))
        , prov_vars_(std::move(pv)), prov_consts_(std::move(pc)) {}

    T value()    const { return value_; }
    T rel_err()  const { return rel_err_bound_; }
    T max_cond() const { return max_cond_seen_; }
    const TrackedId&             id()          const { return id_; }
    const std::set<std::string>& prov_vars()   const { return prov_vars_; }
    const std::set<std::string>& prov_consts() const { return prov_consts_; }

    // Operators — call named free functions with empty source location.
    Tracked operator+(const Tracked& b) const { return tracked::add(*this, b); }
    Tracked operator-(const Tracked& b) const { return tracked::sub(*this, b); }
    Tracked operator*(const Tracked& b) const { return tracked::mul(*this, b); }
    Tracked operator/(const Tracked& b) const { return tracked::div(*this, b); }
    Tracked operator-()                 const { return tracked::neg(*this); }

    Tracked& operator+=(const Tracked& b) { *this = *this + b; return *this; }
    Tracked& operator-=(const Tracked& b) { *this = *this - b; return *this; }
    Tracked& operator*=(const Tracked& b) { *this = *this * b; return *this; }
    Tracked& operator/=(const Tracked& b) { *this = *this / b; return *this; }

    // Comparisons return plain bool — no Tracked propagation.
    bool operator==(const Tracked& b) const { return value_ == b.value_; }
    bool operator!=(const Tracked& b) const { return value_ != b.value_; }
    bool operator< (const Tracked& b) const { return value_ <  b.value_; }
    bool operator> (const Tracked& b) const { return value_ >  b.value_; }
    bool operator<=(const Tracked& b) const { return value_ <= b.value_; }
    bool operator>=(const Tracked& b) const { return value_ >= b.value_; }
};

// ---- Factory functions ------------------------------------------------------

// Tag a fresh *source variable* — an attribution root. Its id is its name and
// it seeds prov_vars.
template <class T>
Tracked<T> track(std::string name, T value) {
    // Build via the full ctor so the one-arg Tracked(T) id-synthesis is not run
    // (and then discarded) — keeps the literal counter sequence stable.
    std::set<std::string> pv{name};
    return Tracked<T>(value, unit_roundoff<T>(), T(0), std::move(name),
                      std::move(pv), {});
}

// Tag a fresh *named constant* — participates in provenance for audit but is not
// an attribution root. Its id is its name and it seeds prov_consts.
template <class T>
Tracked<T> constant(std::string name, T value) {
    // Full ctor: skip the discarded one-arg id-synthesis (see track()).
    std::set<std::string> pc{name};
    return Tracked<T>(value, unit_roundoff<T>(), T(0), std::move(name),
                      {}, std::move(pc));
}

// Anonymous literal: a bare scalar value promoted into the tracked graph.
// Gets an auto-generated id ("_lit@<file>:<line>#<n>[@<scope>]", or "_lit@?#<n>"
// without a source location) so downstream ops can point at it, but is NOT
// classified as a source variable (empty prov_vars) or a named constant
// (empty prov_consts). Analysis tooling can filter or aggregate literals by the
// "_lit@" id prefix.
//
// Use for scratch scalars whose semantic identity doesn't matter and which the
// caller doesn't want to name. If the value has a semantic role, prefer
// constant() (named) or track() (source variable).
template <class T>
Tracked<T> literal(T value, SourceLocation loc = {}) {
    // Full ctor with the located id: constructing through Tracked(T) would first
    // synthesize a no-location "_lit@?#n" id and then overwrite it, double-bumping
    // the counter. Building directly keeps one id per literal() call.
    return Tracked<T>(value, unit_roundoff<T>(), T(0),
                      detail::make_id("_lit", loc), {}, {});
}

// Opaque-call barrier: value crosses a non-Tracked boundary.
//
// Semantics: pass-through.  cond = 1 (we have no insight into what the opaque
// function does, so the conservative null hypothesis is that input errors flow
// through unchanged).  rel_err = max(input_rel_errs) + u (one extra roundoff for
// the boundary crossing).
//
// The record's `in` is [fn_name, input_ids...]: the boundary marker followed by
// the forwarded operand ids so the value graph stays traversable through the
// barrier.  fn_name is a *marker*, not a source variable or constant, so it is
// deliberately kept out of prov_vars / prov_consts (retiring v0.2's behavior of
// folding it into provenance).  A first-class prov_opaque category is a possible
// follow-up (see docs/PROVENANCE.md, "Opaque markers").
//
// Two forms:
//   opaque(fn_name, value)                  -- no tracked inputs.  cond=1,
//                                              rel_err=u, empty provenance.
//   opaque(fn_name, value, in1, in2, ...)   -- preserves error + provenance
//                                              from the tracked inputs that were
//                                              stripped to .value() before the
//                                              external call.

namespace detail {

// Fold tracked inputs into (max_rel_err, union prov_vars/prov_consts, ids).
inline void opaque_fold(double& /*max_err*/, std::set<std::string>& /*pv*/,
                        std::set<std::string>& /*pc*/, std::vector<std::string>& /*ids*/) {}

template <class T, class... Rest>
inline void opaque_fold(double& max_err, std::set<std::string>& pv,
                        std::set<std::string>& pc, std::vector<std::string>& ids,
                        const Tracked<T>& head, const Rest&... rest) {
    if ((double)head.rel_err_bound_ > max_err) max_err = (double)head.rel_err_bound_;
    pv.insert(head.prov_vars_.begin(),   head.prov_vars_.end());
    pc.insert(head.prov_consts_.begin(), head.prov_consts_.end());
    ids.push_back(head.id_);
    opaque_fold(max_err, pv, pc, ids, rest...);
}

} // namespace detail

template <class T, class... Inputs>
Tracked<T> opaque(std::string_view fn_name, T value,
                  const Inputs&... inputs) {
    return opaque_at<T, Inputs...>(fn_name, value, SourceLocation{}, inputs...);
}

// Same, but with an explicit source location.  Use this form when calling from a
// wrapper that wants TRACKED_HERE attribution.
template <class T, class... Inputs>
Tracked<T> opaque_at(std::string_view fn_name, T value, SourceLocation loc,
                     const Inputs&... inputs) {
    double max_in_err = 0.0;
    std::set<std::string> pv, pc;
    std::vector<std::string> in_ids;
    in_ids.push_back(std::string(fn_name));  // boundary marker leads `in`
    detail::opaque_fold(max_in_err, pv, pc, in_ids, inputs...);

    T cond    = T(1);
    T new_err = cond * (T(max_in_err) + unit_roundoff<T>());

    TrackedId id = detail::make_id("opaque", loc);
    journal::emit("opaque", loc, id, in_ids,
                  (double)value, (double)cond, (double)new_err, pv, pc);

    return Tracked<T>(value, new_err, cond, std::move(id), std::move(pv), std::move(pc));
}

// ---- Named free functions: include source location --------------------------
//
// Operators above delegate to these with loc={} (no location).
// For location capture, call directly:
//   auto r = sub(a, b, TRACKED_HERE);
//
// Error bound: new_err = local_cond * (max(input_rel_errs) + unit_roundoff<T>())
// This is the standard first-order model: fl(a op b) = (a op b)(1 + delta), |delta| <= u.
// `in` carries the direct-operand ids verbatim — no primary_id heuristic.

template <class T>
Tracked<T> add(const Tracked<T>& a, const Tracked<T>& b, SourceLocation loc) {
    T res     = a.value_ + b.value_;
    T abs_res = std::abs(res);
    T abs_sum = std::abs(a.value_) + std::abs(b.value_);
    T cond;
    if (a.value_ == -b.value_ && a.value_ != T(0)) {
        // Exact cancellation of x + (-x); result is exactly 0, no precision lost.
        // (The a != 0 guard is required because IEEE has 0 == -0: without it we
        // would conflate 0 + (-0) with meaningful cancellation.)
        cond = T(1);
    } else if (abs_res > T(0)) {
        cond = abs_sum / abs_res;
    } else {
        // abs_res == 0 and not an exact x + (-x) cancellation. abs_sum == 0 iff
        // both operands are zero (0 + 0, 0 + -0) — no precision lost, cond = 1.
        // Otherwise abs_res underflowed to 0 from unequal inputs — genuine loss.
        cond = (abs_sum == T(0)) ? T(1) : T(1) / unit_roundoff<T>();
    }
    T max_in_err = std::max(a.rel_err_bound_, b.rel_err_bound_);
    T new_err    = cond * (max_in_err + unit_roundoff<T>());
    T new_cond   = std::max({a.max_cond_seen_, b.max_cond_seen_, cond});
    TrackedId id = detail::make_id("add", loc);
    auto pv      = detail::prov_union(a.prov_vars_,   b.prov_vars_);
    auto pc      = detail::prov_union(a.prov_consts_, b.prov_consts_);
    journal::emit("add", loc, id, {a.id_, b.id_},
        (double)res, (double)cond, (double)new_err, pv, pc);
    return Tracked<T>(res, new_err, new_cond, std::move(id), std::move(pv), std::move(pc));
}

template <class T>
Tracked<T> sub(const Tracked<T>& a, const Tracked<T>& b, SourceLocation loc) {
    T res     = a.value_ - b.value_;
    T abs_res = std::abs(res);
    T abs_sum = std::abs(a.value_) + std::abs(b.value_);
    T cond;
    if (a.value_ == b.value_) {
        // Exact cancellation: result is exactly 0, no precision lost.
        // (Also covers 0 - 0 and 0 - -0, previously handled by abs_sum == 0.)
        cond = T(1);
    } else if (abs_res > T(0)) {
        cond = abs_sum / abs_res;
    } else {
        // abs_res == 0 but a != b (e.g., subnormal underflow) — genuine loss.
        cond = T(1) / unit_roundoff<T>();
    }
    T max_in_err = std::max(a.rel_err_bound_, b.rel_err_bound_);
    T new_err    = cond * (max_in_err + unit_roundoff<T>());
    T new_cond   = std::max({a.max_cond_seen_, b.max_cond_seen_, cond});
    TrackedId id = detail::make_id("sub", loc);
    auto pv      = detail::prov_union(a.prov_vars_,   b.prov_vars_);
    auto pc      = detail::prov_union(a.prov_consts_, b.prov_consts_);
    journal::emit("sub", loc, id, {a.id_, b.id_},
        (double)res, (double)cond, (double)new_err, pv, pc);
    return Tracked<T>(res, new_err, new_cond, std::move(id), std::move(pv), std::move(pc));
}

template <class T>
Tracked<T> mul(const Tracked<T>& a, const Tracked<T>& b, SourceLocation loc) {
    T res        = a.value_ * b.value_;
    T cond       = T(1);
    T max_in_err = std::max(a.rel_err_bound_, b.rel_err_bound_);
    T new_err    = cond * (max_in_err + unit_roundoff<T>());
    T new_cond   = std::max({a.max_cond_seen_, b.max_cond_seen_, cond});
    TrackedId id = detail::make_id("mul", loc);
    auto pv      = detail::prov_union(a.prov_vars_,   b.prov_vars_);
    auto pc      = detail::prov_union(a.prov_consts_, b.prov_consts_);
    journal::emit("mul", loc, id, {a.id_, b.id_},
        (double)res, (double)cond, (double)new_err, pv, pc);
    return Tracked<T>(res, new_err, new_cond, std::move(id), std::move(pv), std::move(pc));
}

template <class T>
Tracked<T> div(const Tracked<T>& a, const Tracked<T>& b, SourceLocation loc) {
    T res        = a.value_ / b.value_;
    T cond       = T(1);
    T max_in_err = std::max(a.rel_err_bound_, b.rel_err_bound_);
    T new_err    = cond * (max_in_err + unit_roundoff<T>());
    T new_cond   = std::max({a.max_cond_seen_, b.max_cond_seen_, cond});
    TrackedId id = detail::make_id("div", loc);
    auto pv      = detail::prov_union(a.prov_vars_,   b.prov_vars_);
    auto pc      = detail::prov_union(a.prov_consts_, b.prov_consts_);
    journal::emit("div", loc, id, {a.id_, b.id_},
        (double)res, (double)cond, (double)new_err, pv, pc);
    return Tracked<T>(res, new_err, new_cond, std::move(id), std::move(pv), std::move(pc));
}

template <class T>
Tracked<T> neg(const Tracked<T>& a, SourceLocation loc) {
    T res      = -a.value_;
    T cond     = T(1);
    T new_err  = cond * (a.rel_err_bound_ + unit_roundoff<T>());
    T new_cond = std::max(a.max_cond_seen_, cond);
    TrackedId id = detail::make_id("neg", loc);
    auto pv    = a.prov_vars_;
    auto pc    = a.prov_consts_;
    journal::emit("neg", loc, id, {a.id_},
        (double)res, (double)cond, (double)new_err, pv, pc);
    return Tracked<T>(res, new_err, new_cond, std::move(id), std::move(pv), std::move(pc));
}

} // namespace tracked
