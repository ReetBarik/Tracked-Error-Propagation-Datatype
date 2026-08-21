"""Unit tests for the per-statement ``line=`` scope injector (tracked-line-inject).

Ported from the AMP consumer's suite and extended for the library seams
(scope_var_name, extra include dirs/defines).  Two layers:

* the pure-Python text transform (``_instrument_text`` / ``_stmt_span``) — the
  splice logic that is most likely to regress, exercised with hand-built sites
  so it needs neither libclang nor a compiler;
* an end-to-end ``build_line_patch`` over a self-contained C++ snippet (no
  ``#include``s, so no system headers / compiler needed) — guarded by an import
  skip when the libclang wheel is absent.  This pins the structural statement
  selection: declarations wrap with push/pop, other value-producing statements
  wrap with an RAII block, and pure copies / bare declarations are skipped.
"""

from __future__ import annotations

import pytest

from tracked_tools import inject as li


# ---------------------------------------------------------------------------
# pure text transform
# ---------------------------------------------------------------------------

def test_instrument_text_decl_uses_push_pop():
    src = b"    const int a = b * c;\n"
    # a DECL_STMT span includes its trailing ';'
    site = li._Site("f.h", 1, src.index(b"const"), src.index(b";") + 1, is_decl=True)
    out = li._instrument_text(src, [site]).decode()
    assert 'tracked::push_scope("line=f.h:1"); const int a = b * c; tracked::pop_scope();' in out


def test_instrument_text_nondecl_uses_raii_block():
    src = b"    x = y * z;\n"
    start = src.index(b"x")
    site = li._Site("f.h", 7, start, src.index(b";") + 1, is_decl=False)
    out = li._instrument_text(src, [site]).decode()
    assert '{ tracked::scope _tracked_line_scope("line=f.h:7"); x = y * z; }' in out


def test_instrument_text_scope_var_name_is_parameterized():
    """A consumer can reproduce its historical spelling (e.g. AMP's
    _ql_line_scope) exactly through the seam."""
    src = b"    x = y * z;\n"
    site = li._Site("f.h", 7, src.index(b"x"), src.index(b";") + 1, is_decl=False)
    out = li._instrument_text(src, [site], scope_var_name="_ql_line_scope").decode()
    assert '{ tracked::scope _ql_line_scope("line=f.h:7"); x = y * z; }' in out


def test_instrument_text_applies_end_to_start_preserving_offsets():
    src = b"const int a = p*q; r = a*a;\n"
    s1 = li._Site("f.h", 1, 0, src.index(b";") + 1, is_decl=True)
    r_start = src.index(b"r =")
    s2 = li._Site("f.h", 1, r_start, src.index(b";", r_start) + 1, is_decl=False)
    out = li._instrument_text(src, [s1, s2]).decode()
    assert 'push_scope("line=f.h:1"); const int a = p*q; tracked::pop_scope();' in out
    assert '{ tracked::scope _tracked_line_scope("line=f.h:1"); r = a*a; }' in out


# ---------------------------------------------------------------------------
# end-to-end structural selection (needs libclang; no compiler/system headers)
# ---------------------------------------------------------------------------

def _have_clang():
    try:
        import clang.cindex  # noqa: F401
        li._cindex().Index.create()
        return True
    except Exception:
        return False


needs_clang = pytest.mark.skipif(not _have_clang(), reason="libclang unavailable")


@needs_clang
def test_build_line_patch_selects_statements(tmp_path):
    headers = tmp_path / "hdrs"
    headers.mkdir()
    # A function template (dependent types, like real instrumented kernels):
    #  - arithmetic declarations  -> push/pop
    #  - an assignment            -> RAII block
    #  - a pure copy (no op)      -> skipped
    #  - a bare declaration       -> skipped
    #  - a nested block statement -> recursed into
    (headers / "foo.h").write_text(
        "template <class T>\n"
        "T foo(T b, T c, T d) {\n"
        "    T a = b * c;\n"          # L3 decl + op   -> push/pop
        "    T e = d;\n"              # L4 pure copy   -> skipped
        "    T z;\n"                  # L5 bare decl   -> skipped
        "    a = a + e;\n"            # L6 assignment  -> RAII
        "    {\n"
        "        T g = a * d;\n"      # L8 nested decl -> push/pop
        "    }\n"
        "    return a * b;\n"         # L10 return+op  -> RAII
        "}\n",
        encoding="utf-8",
    )
    driver = tmp_path / "drv.cpp"
    driver.write_text('#include "foo.h"\nint main(){ return 0; }\n', encoding="utf-8")

    patch, stats = li.build_line_patch(
        driver_source=driver, headers_dir=headers,
        tracked_include=tmp_path, repo_root=tmp_path,
    )
    assert stats == {"foo.h": 4}          # L3, L6, L8, L10 (copy + bare decl skipped)
    assert patch is not None
    # declarations -> push/pop
    assert 'tracked::push_scope("line=foo.h:3");' in patch
    assert 'tracked::push_scope("line=foo.h:8");' in patch     # nested block recursed
    # non-declarations -> RAII block (default library scope var name)
    assert '{ tracked::scope _tracked_line_scope("line=foo.h:6");' in patch
    assert '{ tracked::scope _tracked_line_scope("line=foo.h:10");' in patch
    # skipped statements carry no scope
    assert "line=foo.h:4" not in patch    # pure copy
    assert "line=foo.h:5" not in patch    # bare declaration
    # basename only, never a path (scope values must not contain '/')
    assert "line=hdrs/foo.h" not in patch


@needs_clang
def test_build_line_patch_multiline_statement_span(tmp_path):
    headers = tmp_path / "hdrs"
    headers.mkdir()
    (headers / "acc.h").write_text(
        "template <class T>\n"
        "T acc(T a, T b, T c) {\n"
        "    T r = (\n"                # L3: multi-line decl
        "        a * b +\n"
        "        b * c\n"
        "    );\n"
        "    return r;\n"
        "}\n",
        encoding="utf-8",
    )
    driver = tmp_path / "drv.cpp"
    driver.write_text('#include "acc.h"\nint main(){ return 0; }\n', encoding="utf-8")
    patch, stats = li.build_line_patch(
        driver_source=driver, headers_dir=headers,
        tracked_include=tmp_path, repo_root=tmp_path,
    )
    # the whole multi-line statement is keyed to its START line and wrapped as one
    assert 'tracked::push_scope("line=acc.h:3");' in patch
    assert stats == {"acc.h": 1}          # the plain 'return r;' has no op -> skipped


@needs_clang
def test_defines_gate_instrumented_code(tmp_path):
    """-D seam: statements behind an #ifdef appear only when the define is passed."""
    headers = tmp_path / "hdrs"
    headers.mkdir()
    (headers / "gate.h").write_text(
        "template <class T>\n"
        "T gate(T a, T b) {\n"
        "#ifdef ENABLE_EXTRA\n"
        "    T x = a * b;\n"           # L4: only with -DENABLE_EXTRA
        "#endif\n"
        "    return a + b;\n"          # L6
        "}\n",
        encoding="utf-8",
    )
    driver = tmp_path / "drv.cpp"
    driver.write_text('#include "gate.h"\nint main(){ return 0; }\n', encoding="utf-8")

    _, stats_off = li.build_line_patch(
        driver_source=driver, headers_dir=headers,
        tracked_include=tmp_path, repo_root=tmp_path,
    )
    assert stats_off == {"gate.h": 1}

    patch_on, stats_on = li.build_line_patch(
        driver_source=driver, headers_dir=headers,
        tracked_include=tmp_path, repo_root=tmp_path,
        defines=["ENABLE_EXTRA"],
    )
    assert stats_on == {"gate.h": 2}
    assert 'line=gate.h:4' in patch_on
