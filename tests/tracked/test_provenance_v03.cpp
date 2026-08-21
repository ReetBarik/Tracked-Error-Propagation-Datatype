#include <catch2/catch_test_macros.hpp>
#include <catch2/catch_approx.hpp>

#include <tracked/tracked.hpp>
#include <tracked/ops.hpp>

#include <algorithm>
#include <cstdio>
#include <fstream>
#include <regex>
#include <set>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

using Catch::Approx;
using tracked::track;
using tracked::constant;
using tracked::Tracked;
using tracked::journal::records;
using tracked::journal::clear;

// ---- helpers ----------------------------------------------------------------

static bool ends_with(const std::string& s, const std::string& suf) {
    return s.size() >= suf.size() && s.compare(s.size() - suf.size(), suf.size(), suf) == 0;
}

// A fixed call site so the (file,line,op) callsite counter is shared across
// invocations — used by the counter tests.
static Tracked<double> sub_here(const Tracked<double>& a, const Tracked<double>& b) {
    return tracked::sub(a, b, TRACKED_HERE);
}

static long pos_of(const std::vector<std::string>& v, const std::string& id) {
    for (long i = 0; i < (long)v.size(); ++i) if (v[i] == id) return i;
    return -1;
}

// ============================================================
// 1–2. Factories: two provenance categories
// ============================================================

TEST_CASE("track_sets_id_and_prov_vars") {
    auto x = track("m1", 1.0);
    REQUIRE(x.id() == "m1");
    REQUIRE(x.prov_vars()   == std::set<std::string>{"m1"});
    REQUIRE(x.prov_consts().empty());
}

TEST_CASE("constant_sets_id_and_prov_consts") {
    auto c = constant("pi", 3.14);
    REQUIRE(c.id() == "pi");
    REQUIRE(c.prov_vars().empty());
    REQUIRE(c.prov_consts() == std::set<std::string>{"pi"});
}

// ============================================================
// 3. Derived values get a generated id
// ============================================================

TEST_CASE("derived_value_has_generated_id") {
    clear();
    auto x = track("m1", 1.0);
    auto c = constant("two", 2.0);
    auto r = tracked::sub(x, c, TRACKED_HERE);

    // id shape: <op>@<file>:<line>#<counter>
    std::regex shape(R"(^sub@.*:[0-9]+#[0-9]+$)");
    REQUIRE(std::regex_search(r.id(), shape));
    // provenance correctly split and unioned
    REQUIRE(r.prov_vars()   == std::set<std::string>{"m1"});
    REQUIRE(r.prov_consts() == std::set<std::string>{"two"});
}

// ============================================================
// 4. Callsite counter increments at a shared site
// ============================================================

TEST_CASE("callsite_counter_increments") {
    clear();
    auto a = track("a", 3.0);
    auto b = track("b", 1.0);
    auto r1 = sub_here(a, b);   // #1
    auto r2 = sub_here(a, b);   // #2 (same file:line:op)
    auto r3 = sub_here(a, b);   // #3
    REQUIRE(ends_with(r1.id(), "#1"));
    REQUIRE(ends_with(r2.id(), "#2"));
    REQUIRE(ends_with(r3.id(), "#3"));
}

// ============================================================
// 5. Callsite counter is per-thread
// ============================================================

TEST_CASE("callsite_counter_per_thread") {
    std::string id_a, id_b;
    std::thread ta([&] {
        clear();                       // thread-local: resets this thread's counter
        auto a = track("a", 3.0);
        auto b = track("b", 1.0);
        id_a = sub_here(a, b).id();    // first op in this thread -> #1
    });
    std::thread tb([&] {
        clear();
        auto a = track("a", 3.0);
        auto b = track("b", 1.0);
        id_b = sub_here(a, b).id();    // first op in this thread -> #1
    });
    ta.join();
    tb.join();
    REQUIRE(ends_with(id_a, "#1"));
    REQUIRE(ends_with(id_b, "#1"));
}

// ============================================================
// 6. Scope appends a suffix (nested scopes join with '/')
// ============================================================

TEST_CASE("scope_appends_suffix") {
    clear();
    auto a = track("a", 3.0);
    auto b = track("b", 1.0);

    {
        tracked::scope s("s=1");
        auto r = tracked::sub(a, b, TRACKED_HERE);
        REQUIRE(ends_with(r.id(), "@s=1"));
    }
    {
        tracked::scope outer("run=A");
        tracked::scope inner("s=1");
        auto r = tracked::sub(a, b, TRACKED_HERE);
        REQUIRE(ends_with(r.id(), "@run=A/s=1"));
    }
    // scope popped: no suffix again
    {
        auto r = tracked::sub(a, b, TRACKED_HERE);
        REQUIRE(!ends_with(r.id(), "@s=1"));
    }
}

// ============================================================
// 6b. Free push_scope/pop_scope wrap the same stack (no block)
// ============================================================

TEST_CASE("push_pop_scope_suffix") {
    clear();
    auto a = track("a", 3.0);
    auto b = track("b", 1.0);

    // A declaration statement can be wrapped without a lexical block: `x`
    // remains visible after pop_scope(), yet the op inside carries the suffix.
    tracked::push_scope("line=f.h:10");
    auto x = tracked::sub(a, b, TRACKED_HERE);   // ends with @line=f.h:10
    tracked::pop_scope();
    REQUIRE(ends_with(x.id(), "@line=f.h:10"));

    // popped: subsequent ops are unscoped again, and `x` is still usable here
    auto y = tracked::sub(x, b, TRACKED_HERE);
    REQUIRE(!ends_with(y.id(), "@line=f.h:10"));

    // push/pop nests under an enclosing RAII scope, joining with '/'
    {
        tracked::scope outer("run=A");
        tracked::push_scope("line=f.h:20");
        auto r = tracked::sub(a, b, TRACKED_HERE);
        tracked::pop_scope();
        REQUIRE(ends_with(r.id(), "@run=A/line=f.h:20"));
        // back to just the outer scope after pop
        auto r2 = tracked::sub(a, b, TRACKED_HERE);
        REQUIRE(ends_with(r2.id(), "@run=A"));
    }
}

// ============================================================
// 7. Journal flush emits the v0.3 schema
// ============================================================

TEST_CASE("journal_emits_new_schema") {
    clear();
    auto m1 = track("m1", 10.0);
    auto two = constant("two", 2.0);
    auto r = tracked::div(m1, two, TRACKED_HERE);

    const std::string path = "test_provenance_v03_schema.jsonl";
    tracked::journal::flush(path);

    std::ifstream in(path);
    std::stringstream ss;
    ss << in.rdbuf();
    std::string content = ss.str();
    std::remove(path.c_str());

    // new field names present...
    REQUIRE(content.find("\"id\":") != std::string::npos);
    REQUIRE(content.find("\"prov_vars\":") != std::string::npos);
    REQUIRE(content.find("\"prov_consts\":") != std::string::npos);
    // ...populated correctly...
    REQUIRE(content.find("\"prov_vars\":[\"m1\"]") != std::string::npos);
    REQUIRE(content.find("\"prov_consts\":[\"two\"]") != std::string::npos);
    // ...and the old flat field is gone.
    REQUIRE(content.find("\"prov\":") == std::string::npos);
}

// ============================================================
// 8. THE regression test: `in` carries direct-operand ids, not a constant name
// ============================================================

TEST_CASE("in_field_carries_direct_operand_ids") {
    // Pre-v0.3 bug: in was primary_id(operand) = alphabetically-first provenance
    // name. Underscore-prefixed constants (_four, _half) sorted before real
    // variables, so the outer sub reported in=[_four, m1] — a constant, not the
    // actual mul-result operand. v0.3 records the operand's own id verbatim.
    clear();
    auto m1   = track("m1", 10.0);
    auto four = constant("_four", 4.0);
    auto half = constant("_half", 0.5);

    auto prod = tracked::mul(four, half, TRACKED_HERE);  // 2.0
    auto y    = tracked::sub(m1, prod, TRACKED_HERE);    // 8.0

    const auto& rec = records().back();                  // the outer sub
    REQUIRE(rec.op == "sub");
    REQUIRE(rec.in == (std::vector<std::string>{"m1", prod.id()}));

    // The bug specifically: the constant name must NOT appear in `in`.
    REQUIRE(std::find(rec.in.begin(), rec.in.end(), "_four") == rec.in.end());
    REQUIRE(std::find(rec.in.begin(), rec.in.end(), "_half") == rec.in.end());
    // The operand id is the mul-result's generated id.
    REQUIRE(std::regex_search(prod.id(), std::regex(R"(^mul@.*#[0-9]+$)")));
}

// ============================================================
// 9. trace_sources returns variable roots only (constants excluded)
// ============================================================

TEST_CASE("trace_sources_returns_variable_roots") {
    clear();
    auto m1   = track("m1", 10.0);
    auto four = constant("_four", 4.0);
    auto half = constant("_half", 0.5);
    auto prod = tracked::mul(four, half, TRACKED_HERE);
    auto y    = tracked::sub(m1, prod, TRACKED_HERE);

    auto sources = tracked::journal::trace_sources(y.id());
    REQUIRE(sources == std::set<std::string>{"m1"});   // constants excluded
}

// ============================================================
// 10. trace_ancestors returns causal-topological order
// ============================================================

TEST_CASE("trace_ancestors_topological_order") {
    clear();
    auto a = track("a", 1.0);
    auto b = track("b", 2.0);
    auto c = track("c", 3.0);
    auto r1 = tracked::add(a, b, TRACKED_HERE);   // a,b -> r1
    auto r2 = tracked::mul(r1, c, TRACKED_HERE);  // r1,c -> r2
    auto r3 = tracked::sub(r2, a, TRACKED_HERE);  // r2,a -> r3

    auto order = tracked::journal::trace_ancestors(r3.id());

    // Every id must appear after all of its parents.
    REQUIRE(pos_of(order, a.id())  < pos_of(order, r1.id()));
    REQUIRE(pos_of(order, b.id())  < pos_of(order, r1.id()));
    REQUIRE(pos_of(order, r1.id()) < pos_of(order, r2.id()));
    REQUIRE(pos_of(order, c.id())  < pos_of(order, r2.id()));
    REQUIRE(pos_of(order, r2.id()) < pos_of(order, r3.id()));
    REQUIRE(pos_of(order, a.id())  < pos_of(order, r3.id()));
    // r3 (the query) is last.
    REQUIRE(order.back() == r3.id());
}

// ============================================================
// 11. Opaque marker handling (deferred design — option 3)
// ============================================================
//
// The opaque fn_name marker is neither a source variable nor a named constant,
// so v0.3 keeps it out of prov_vars/prov_consts and out of trace_sources. It
// survives as the leading entry of `in` (so the value graph stays traversable),
// but a first-class prov_opaque category is deferred to a follow-up. This test
// guards that deferred contract; see docs/PROVENANCE.md, "Opaque markers".

TEST_CASE("opaque_marker_excluded_from_provenance_and_sources") {
    clear();
    auto m1 = track("m1", 2.0);
    auto y  = tracked::opaque_at<double>("vendor_fn", 5.0, TRACKED_HERE, m1);

    // Marker leads `in`, followed by the forwarded operand id.
    REQUIRE(records().back().in == (std::vector<std::string>{"vendor_fn", "m1"}));
    // Marker is not provenance.
    REQUIRE(y.prov_vars()   == std::set<std::string>{"m1"});
    REQUIRE(y.prov_consts().empty());
    // Graph walk sees through the boundary to the real source, ignoring the marker.
    REQUIRE(tracked::journal::trace_sources(y.id()) == std::set<std::string>{"m1"});
}
