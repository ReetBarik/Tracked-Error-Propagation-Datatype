// The consumer-header constexpr pattern (qcdloop's kokkosMaths.h): small
// helper functions declared `constexpr` inside a class template carry
// line-scope instrumentation, but with T = Tracked<double> they are only ever
// evaluated at runtime.  Clang — unlike GCC — checks constexpr function bodies
// at *definition time* for locals of non-dependent, non-literal type, so merely
// writing `tracked::scope` inside such a helper is a compile error unless
// `scope` is a literal type (TRACKED_CONSTEXPR20 in tracked.hpp).  This file is
// compiled at C++20 (target_compile_features in CMakeLists.txt); the patterns
// below ARE the test — the file fails to compile if scope loses literal-type
// status.  The runtime sections assert the push/pop still actually happen.

#include <catch2/catch_test_macros.hpp>
#include <tracked/tracked.hpp>

#if defined(__cpp_constexpr) && __cpp_constexpr >= 201907L \
    && defined(__cpp_lib_is_constant_evaluated) \
    && defined(__cpp_lib_constexpr_string) && __cpp_lib_constexpr_string >= 201907L

namespace {

using tracked::Tracked;

template <class T>
struct kernel_constants {
    // qcdloop pattern 1: RAII scope + return inside a constexpr member
    // (kokkosMaths.h:196 `{ tracked::scope _ql_line_scope(...); return T(…); }`).
    static constexpr T eps_sum(const T& a, const T& b) {
        tracked::scope _line("line=kconst.h:196");
        return add(a, b, TRACKED_HERE);
    }

    // qcdloop pattern 2: free push/pop wrapping a declaration statement
    // (kokkosMaths.h:28 `tracked::push_scope(...); constexpr double coeffs[…]`).
    static constexpr double coeff(int i) {
        tracked::push_scope("line=kconst.h:28"); constexpr double coeffs[3] = {1.5, 2.5, 3.5}; tracked::pop_scope();
        return coeffs[i];
    }
};

// A plain (non-template) constexpr function with a scope local must be
// genuinely constant-evaluable: push/pop are no-ops during constant evaluation.
constexpr double scoped_identity(double x) {
    tracked::scope _s("k=v");
    return x;
}
static_assert(scoped_identity(2.5) == 2.5,
              "scope must be a no-op during constant evaluation");

} // namespace

TEST_CASE("scope inside a constexpr function still pushes/pops at runtime") {
    tracked::journal::clear();

    auto a = tracked::track("a", 1.0);
    auto b = tracked::track("b", 2.0);
    auto r = kernel_constants<Tracked<double>>::eps_sum(a, b);
    REQUIRE(r.value_ == 3.0);

    const auto& recs = tracked::journal::records();
    REQUIRE(recs.size() == 1);
    CHECK(recs[0].op == "add");
    // The scope was live for the add's generated id …
    CHECK(recs[0].id.find("@line=kconst.h:196") != std::string::npos);

    // … and popped again on return.
    auto c = add(a, b, TRACKED_HERE);
    (void)c;
    CHECK(tracked::journal::records().back().id.find("@line=") == std::string::npos);
}

TEST_CASE("push_scope/pop_scope inside a constexpr function at runtime") {
    CHECK(kernel_constants<Tracked<double>>::coeff(1) == 2.5);
}

TEST_CASE("scope in a constexpr function evaluated at runtime is unchanged") {
    // The compile-time path is asserted by the static_assert above; the same
    // call at runtime takes the normal push/pop path.
    CHECK(scoped_identity(3.5) == 3.5);
}

#else

TEST_CASE("scope constexpr-context support (inactive at this standard)") {
    SUCCEED("compiled without C++20 constexpr string support; "
            "TRACKED_CONSTEXPR20 is a no-op here");
}

#endif
