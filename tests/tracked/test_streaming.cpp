// Phase-3 streaming contract (docs/STREAMING.md): flush_and_clear writes
// incrementally with one header per stream, clears the buffer, and does NOT
// reset the id counters — so a chunked run's stream is byte-identical to the
// monolithic run's journal.

#include <catch2/catch_test_macros.hpp>

#include <tracked/tracked.hpp>
#include <tracked/ops.hpp>

#include <cstdio>
#include <fstream>
#include <sstream>
#include <string>

using tracked::track;
using tracked::journal::clear;
using tracked::journal::records;

namespace {

void run_sample(int i) {
    tracked::scope s("sample=" + std::to_string(i));
    auto a = track("a", 1.0 + i);
    auto b = track("b", 2.0);
    auto r = tracked::sqrt(a + b, TRACKED_HERE);   // one anon op + one located op
    (void)r;
}

std::size_t count_headers(const std::string& s) {
    std::size_t n = 0, pos = 0;
    while ((pos = s.find("{\"schema\":", pos)) != std::string::npos) { ++n; ++pos; }
    return n;
}

} // namespace

TEST_CASE("chunked flush_and_clear equals monolithic flush byte-for-byte") {
    clear();
    std::ostringstream chunked;
    for (int i = 0; i < 3; ++i) {
        run_sample(i);
        tracked::journal::flush_and_clear(chunked);
        REQUIRE(records().empty());               // buffer bounded to one chunk
    }
    REQUIRE(count_headers(chunked.str()) == 1);   // header once per stream

    clear();                                      // fresh run: counters replay
    for (int i = 0; i < 3; ++i) run_sample(i);
    const std::string path = "test_streaming_mono.jsonl";
    tracked::journal::flush(path);
    std::ifstream in(path);
    std::stringstream mono;
    mono << in.rdbuf();
    std::remove(path.c_str());

    REQUIRE(chunked.str() == mono.str());
}

TEST_CASE("flush_and_clear keeps counters; clear() resets them") {
    clear();
    std::ostringstream out;
    run_sample(0);
    std::string first_sqrt_id;
    for (const auto& r : records())
        if (r.op == "sqrt") first_sqrt_id = r.id;
    REQUIRE(first_sqrt_id.find("#1@") != std::string::npos);

    tracked::journal::flush_and_clear(out);
    run_sample(1);                                // counters continue: #2
    bool found = false;
    for (const auto& r : records())
        if (r.op == "sqrt") {
            REQUIRE(r.id.find("#2@") != std::string::npos);
            found = true;
        }
    REQUIRE(found);

    clear();                                      // run boundary: counters reset
    run_sample(2);
    for (const auto& r : records())
        if (r.op == "sqrt")
            REQUIRE(r.id.find("#1@") != std::string::npos);
}
