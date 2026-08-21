// Viewer-fixture driver: runs the example kernels (kernels.hpp) under the
// chunkable-driver contract (docs/STREAMING.md) and writes a v1-schema
// journal.  This is the recipe the committed fixtures under viewer/fixtures/
// were generated with — see viewer/fixtures/README.md for the exact commands.
//
// Contract obligations this driver satisfies:
//   1. Pure per-sample inputs: every kernel's inputs are closed-form functions
//      of the GLOBAL sample index, so --sample-offset needs no RNG refill and
//      any slice is bit-reproducible ("--unit u --sample-offset i
//      --sample-count 1" is the viewer's drill-down rerun).
//   2. Scoped samples: every (unit, sample) runs under
//      "integral=<unit>" / "sample=<global index>" scopes.
//   3. Sample contiguity per stream: serial execution, RAII scopes.
//   4. Chunked flushing: --chunk-size N flushes via journal::flush_and_clear
//      every N samples — byte-identical output to the monolithic run (a ctest
//      asserts this; see chunk_equality_test.cmake).
//
// Shards produced with --sample-offset/--sample-count have disjoint sample
// scopes, so their files may be reduced-and-merged or concatenated per the
// docs/SCHEMA.md concatenation rule.

#include "kernels.hpp"

#include <tracked/journal.hpp>

#include <cstdio>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

namespace {

int usage(const char* argv0) {
    std::fprintf(stderr,
        "usage: %s [--out PATH] [--sample-offset N] [--sample-count N]\n"
        "          [--chunk-size N] [--unit NAME]... [--list-units]\n"
        "\n"
        "  --out PATH        output journal (default: journal.jsonl)\n"
        "  --sample-offset N first global sample index (default: 0)\n"
        "  --sample-count N  samples per unit (default: 32)\n"
        "  --chunk-size N    flush_and_clear every N samples (0 = one flush\n"
        "                    at exit; any N yields byte-identical output)\n"
        "  --unit NAME       run only this unit (repeatable; default: all)\n"
        "  --list-units      print unit names and exit\n",
        argv0);
    return 2;
}

} // namespace

int main(int argc, char** argv) {
    std::string out = "journal.jsonl";
    long offset = 0, count = 32, chunk = 0;
    std::vector<std::string> only;

    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        auto next = [&]() -> const char* {
            return (i + 1 < argc) ? argv[++i] : nullptr;
        };
        if (arg == "--list-units") {
            for (const auto& u : viewer_kernels::kUnits)
                std::printf("%s\n", u.name);
            return 0;
        } else if (arg == "--out") {
            const char* v = next(); if (!v) return usage(argv[0]); out = v;
        } else if (arg == "--sample-offset") {
            const char* v = next(); if (!v) return usage(argv[0]); offset = std::atol(v);
        } else if (arg == "--sample-count") {
            const char* v = next(); if (!v) return usage(argv[0]); count = std::atol(v);
        } else if (arg == "--chunk-size") {
            const char* v = next(); if (!v) return usage(argv[0]); chunk = std::atol(v);
        } else if (arg == "--unit") {
            const char* v = next(); if (!v) return usage(argv[0]); only.emplace_back(v);
        } else {
            std::fprintf(stderr, "unknown argument: %s\n", arg.c_str());
            return usage(argv[0]);
        }
    }
    if (offset < 0 || count < 0 || chunk < 0) return usage(argv[0]);
    for (const auto& name : only) {
        bool known = false;
        for (const auto& u : viewer_kernels::kUnits)
            if (name == u.name) { known = true; break; }
        if (!known) {
            std::fprintf(stderr, "unknown unit: %s (see --list-units)\n",
                         name.c_str());
            return 2;
        }
    }

    // One checked output path for both modes: flush_and_clear on a fresh
    // stream is byte-identical to journal::flush(path) (docs/STREAMING.md;
    // the chunk-equality ctest pins it), and unlike flush(path) it lets the
    // driver detect an unwritable/failed output instead of exiting 0.
    std::ofstream stream(out, std::ios::trunc);
    if (!stream) {
        std::fprintf(stderr, "cannot open %s\n", out.c_str());
        return 1;
    }

    long total = 0, since_flush = 0;
    for (const auto& u : viewer_kernels::kUnits) {
        if (!only.empty()) {
            bool selected = false;
            for (const auto& name : only)
                if (name == u.name) { selected = true; break; }
            if (!selected) continue;
        }
        tracked::scope unit_scope(std::string("integral=") + u.name);
        for (long i = offset; i < offset + count; ++i) {
            {
                tracked::scope sample_scope("sample=" + std::to_string(i));
                u.fn(static_cast<int>(i));
            }
            ++total;
            if (chunk > 0 && ++since_flush >= chunk) {
                tracked::journal::flush_and_clear(stream);
                since_flush = 0;
            }
        }
    }

    const std::size_t buffered = tracked::journal::records().size();
    tracked::journal::flush_and_clear(stream);
    if (!stream) {
        std::fprintf(stderr, "write failed: %s\n", out.c_str());
        return 1;
    }
    if (chunk > 0)
        std::printf("%s: %ld samples (chunked, chunk-size %ld)\n",
                    out.c_str(), total, chunk);
    else
        std::printf("%s: %ld samples, %zu records\n", out.c_str(), total,
                    buffered);
    return 0;
}
