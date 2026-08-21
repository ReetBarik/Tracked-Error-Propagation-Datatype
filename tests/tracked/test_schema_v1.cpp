// Journal schema v1 (docs/SCHEMA.md): header record, cap causes, exact_tie,
// non-finite string sentinels, emitter-side JSON escaping, validated scopes.

#include <catch2/catch_test_macros.hpp>
#include <catch2/catch_approx.hpp>

#include <tracked/tracked.hpp>
#include <tracked/ops.hpp>
#include <tracked/version.hpp>

#include <cstdio>
#include <fstream>
#include <limits>
#include <sstream>
#include <string>
#include <vector>

using tracked::track;
using tracked::journal::clear;
using tracked::journal::records;
using Catch::Approx;

namespace {

const double kInf = std::numeric_limits<double>::infinity();
const double kNaN = std::numeric_limits<double>::quiet_NaN();

std::vector<std::string> flush_lines() {
    const std::string path = "test_schema_v1_tmp.jsonl";
    tracked::journal::flush(path);
    std::ifstream in(path);
    std::vector<std::string> lines;
    std::string line;
    while (std::getline(in, line)) lines.push_back(line);
    std::remove(path.c_str());
    return lines;
}

} // namespace

// ---- header record ----------------------------------------------------------

TEST_CASE("flush writes the v1 header as line 1, even for an empty journal") {
    clear();
    auto lines = flush_lines();
    REQUIRE(lines.size() == 1);
    REQUIRE(lines[0].find("{\"schema\":1,") == 0);
    REQUIRE(lines[0].find("\"library_version\":\"" TRACKED_VERSION_STRING "\"")
            != std::string::npos);
    REQUIRE(lines[0].find("\"keys\":[\"op\",\"at\",\"id\",\"in\",\"val\","
                          "\"cond\",\"rel_err\",\"prov_vars\",\"prov_consts\"]")
            != std::string::npos);

    auto a = track("a", 1.0);
    auto r = a + a;
    (void)r;
    lines = flush_lines();
    REQUIRE(lines.size() == 2);
    REQUIRE(lines[0].find("\"schema\":1") != std::string::npos);
    REQUIRE(lines[1].find("\"op\":\"add\"") != std::string::npos);
}

// ---- cap causes --------------------------------------------------------------

TEST_CASE("each saturating branch emits its cap cause") {
    clear();
    auto one = track("one", 1.0);
    auto z   = track("z", 0.0);
    auto p   = track("p", 3.141592653589793);

    tracked::log(one, TRACKED_HERE);          // |ln 1| = 0 <= u
    REQUIRE(records().back().cap == "log");

    tracked::sin(p, TRACKED_HERE);            // |sin pi| < u*pi
    REQUIRE(records().back().cap == "sin");

    auto half_pi = track("hp", 1.5707963267948966);
    tracked::cos(half_pi, TRACKED_HERE);      // |cos pi/2| < u*(pi/2)
    REQUIRE(records().back().cap == "cos");

    tracked::atan2(z, one, TRACKED_HERE);     // |atan2(0,1)| = 0 < u
    REQUIRE(records().back().cap == "atan2");

    // all four report the saturated cond 1/u
    double inv_u = 1.0 / tracked::unit_roundoff<double>();
    for (const auto& r : records()) REQUIRE(r.cond == inv_u);
}

TEST_CASE("uncapped ops emit no cap") {
    clear();
    auto a = track("a", 2.0);
    tracked::log(a, TRACKED_HERE);
    tracked::sqrt(a, TRACKED_HERE);
    auto r = a + a;
    (void)r;
    for (const auto& rec : records()) REQUIRE(rec.cap.empty());
}

TEST_CASE("NaN falls through saturation guards and is labeled cap=nan") {
    clear();
    auto n   = track("n", kNaN);
    auto one = track("one", 1.0);
    auto neg = track("neg", -1.0);
    double inv_u = 1.0 / tracked::unit_roundoff<double>();

    auto s1 = n + one;                        // add(NaN, 1)
    REQUIRE(records().back().cap == "nan");
    REQUIRE(records().back().cond == inv_u);
    REQUIRE_FALSE(records().back().exact_tie);
    (void)s1;

    auto s2 = n - one;                        // sub(NaN, 1)
    REQUIRE(records().back().cap == "nan");
    (void)s2;

    tracked::log(neg, TRACKED_HERE);          // log(-1) = NaN: not "x ~ 1"
    REQUIRE(records().back().cap == "nan");

    auto inf = track("inf", kInf);
    tracked::sin(inf, TRACKED_HERE);          // sin(inf) = NaN
    REQUIRE(records().back().cap == "nan");
}

TEST_CASE("non-finite ties saturate with cap=nan instead of reporting cond 1") {
    clear();
    auto pinf = track("pinf", kInf);
    auto ninf = track("ninf", -kInf);
    double inv_u = 1.0 / tracked::unit_roundoff<double>();

    auto s = pinf + ninf;                     // inf + (-inf) = NaN
    REQUIRE(std::isnan(s.value()));
    REQUIRE(records().back().cap == "nan");
    REQUIRE(records().back().cond == inv_u);
    REQUIRE_FALSE(records().back().exact_tie);

    auto d = pinf - pinf;                     // inf - inf = NaN
    REQUIRE(std::isnan(d.value()));
    REQUIRE(records().back().cap == "nan");
    REQUIRE_FALSE(records().back().exact_tie);
}

// ---- exact_tie ---------------------------------------------------------------

TEST_CASE("finite exact ties are marked; ordinary ops are not") {
    clear();
    auto a = track("a", 3.5);
    auto na = track("na", -3.5);
    auto z = track("z", 0.0);
    auto nz = track("nz", -0.0);

    auto t1 = a - a;                          // a - a
    REQUIRE(t1.value() == 0.0);
    REQUIRE(records().back().exact_tie);
    REQUIRE(records().back().cond == 1.0);
    REQUIRE(records().back().cap.empty());

    auto t2 = a + na;                         // a + (-a), a != 0
    REQUIRE(records().back().exact_tie);

    auto t3 = z + nz;                         // 0 + (-0): both-zero tie
    REQUIRE(records().back().exact_tie);
    REQUIRE(records().back().cond == 1.0);

    auto t4 = z - nz;                         // 0 - (-0): a == b holds (0 == -0)
    REQUIRE(records().back().exact_tie);

    auto t5 = a + a;                          // ordinary add
    REQUIRE_FALSE(records().back().exact_tie);
    (void)t1; (void)t2; (void)t3; (void)t4; (void)t5;
}

// ---- non-finite sentinels ------------------------------------------------------

TEST_CASE("val/cond/rel_err encode NaN and +/-Inf as string sentinels") {
    clear();
    auto z = track("z", 0.0);
    auto h = track("h", 1e308);
    auto n = track("n", -1e308);

    auto nan_v = z / z;                       // val = NaN
    auto inf_v = h + h;                       // val = +Inf
    auto ninf_v = n + n;                      // val = -Inf
    (void)nan_v; (void)inf_v; (void)ninf_v;

    auto lines = flush_lines();
    REQUIRE(lines.size() == 4);
    REQUIRE(lines[1].find("\"val\":\"nan\"") != std::string::npos);
    REQUIRE(lines[2].find("\"val\":\"inf\"") != std::string::npos);
    REQUIRE(lines[3].find("\"val\":\"-inf\"") != std::string::npos);
    // the legacy encodings are gone
    for (const auto& l : lines) {
        REQUIRE(l.find("null") == std::string::npos);
        REQUIRE(l.find("1.7976931348623157e+308") == std::string::npos);
    }
    // inf + inf overflow also poisons the add's measured cond (inf/inf = NaN)
    REQUIRE(lines[2].find("\"cond\":\"nan\"") != std::string::npos);
}

// ---- emitter-side JSON escaping ------------------------------------------------

TEST_CASE("user-chosen names are JSON-escaped, not structure-forging") {
    clear();
    auto q = track("a\",\"b", 1.0);           // quote-injection attempt
    auto r = q + q;
    (void)r;
    auto lines = flush_lines();
    REQUIRE(lines.size() == 2);
    // the name lands escaped: a\",\"b  — one array element, not two
    REQUIRE(lines[1].find("\"in\":[\"a\\\",\\\"b\",\"a\\\",\\\"b\"]")
            != std::string::npos);
    REQUIRE(lines[1].find("\"prov_vars\":[\"a\\\",\\\"b\"]") != std::string::npos);

    clear();
    auto nl = track("line\nbreak\ttab", 2.0);
    auto r2 = nl + nl;
    (void)r2;
    lines = flush_lines();
    REQUIRE(lines.size() == 2);               // the newline did NOT split the line
    REQUIRE(lines[1].find("line\\nbreak\\ttab") != std::string::npos);
}

// ---- scope validation ----------------------------------------------------------

TEST_CASE("push_scope and scope validate the key=value grammar") {
    REQUIRE_NOTHROW(tracked::push_scope("integral=B15"));
    tracked::pop_scope();
    REQUIRE_NOTHROW(tracked::push_scope("line=f.h:10"));
    tracked::pop_scope();

    REQUIRE_THROWS_AS(tracked::push_scope("noequals"), std::invalid_argument);
    REQUIRE_THROWS_AS(tracked::push_scope("=v"), std::invalid_argument);
    REQUIRE_THROWS_AS(tracked::push_scope("k="), std::invalid_argument);
    REQUIRE_THROWS_AS(tracked::push_scope("k=a/b"), std::invalid_argument);
    REQUIRE_THROWS_AS(tracked::push_scope("k=a=b"), std::invalid_argument);
    REQUIRE_THROWS_AS(tracked::scope("bad scope"), std::invalid_argument);
    REQUIRE_NOTHROW(tracked::scope("run=A"));
}
