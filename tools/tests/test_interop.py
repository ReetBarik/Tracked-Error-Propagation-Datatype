"""Interop-kit tests: the SOURCE_HASH cache discipline, the packaged ruleset
(byte-pinned so consumers' cached shims stay valid), and the LLM plumbing
utilities (no live LLM anywhere).
"""

from __future__ import annotations

from tracked_tools.interop import cache, llm, ruleset_text

# The consumer-side (AMP) locked value: the ruleset must ship byte-identical
# or every cached shim hashed against it silently invalidates at migration.
AMP_RULESET_HASH = "473ccee3385392101f03d66f7d3fe8f6be11b3a57c38d9abe16e4b7a65fc914c"


def test_packaged_ruleset_hash_is_pinned():
    assert cache.ruleset_hash(ruleset_text()) == AMP_RULESET_HASH


# ---------------------------------------------------------------------------
# SOURCE_HASH cache discipline
# ---------------------------------------------------------------------------

def _tree(tmp_path):
    d = tmp_path / "hdrs"
    (d / "sub").mkdir(parents=True)
    (d / "a.h").write_text("int a;\n", encoding="utf-8")
    (d / "sub" / "b.hpp").write_text("int b;\n", encoding="utf-8")
    (d / "README.md").write_text("docs\n", encoding="utf-8")
    return d


def test_hash_header_dir_sensitivity(tmp_path):
    d = _tree(tmp_path)
    h0 = cache.hash_header_dir(d)

    # non-header churn is invisible
    (d / "README.md").write_text("docs v2\n", encoding="utf-8")
    assert cache.hash_header_dir(d) == h0

    # a header edit changes it
    (d / "a.h").write_text("int a2;\n", encoding="utf-8")
    h1 = cache.hash_header_dir(d)
    assert h1 != h0

    # a rename changes it (path is folded in)
    (d / "a.h").rename(d / "a2.h")
    assert cache.hash_header_dir(d) not in (h0, h1)


def test_compute_source_hash_folds_ruleset(tmp_path):
    d = _tree(tmp_path)
    h_rules1 = cache.compute_source_hash(d, "ruleset v1")
    h_rules2 = cache.compute_source_hash(d, "ruleset v2")
    assert h_rules1 != h_rules2          # a rule refinement invalidates
    assert h_rules1 == cache.compute_source_hash(d, "ruleset v1")


def test_extract_and_apply_source_hash():
    assert cache.extract_source_hash("// no hash here\n") is None
    assert cache.extract_source_hash("// SOURCE_HASH: PENDING\n") is None
    assert cache.extract_source_hash("x\n// SOURCE_HASH: abc123\ny\n") == "abc123"

    stamped = cache.apply_source_hash("hdr\n// SOURCE_HASH: PENDING\nbody\n", "abc")
    assert "// SOURCE_HASH: abc" in stamped and "PENDING" not in stamped

    # a generator that omitted the line gets one injected after line 1
    injected = cache.apply_source_hash("line0\nline1\n", "def")
    assert injected.splitlines()[1] == "// SOURCE_HASH: def"


def test_region_hash_is_order_independent_in_writes():
    h1 = cache.compute_region_hash("src", "rules", "float-float", ["b", "a"])
    h2 = cache.compute_region_hash("src", "rules", "float-float", ["a", "b"])
    assert h1 == h2
    assert h1 != cache.compute_region_hash("src", "rules", "double-double", ["a", "b"])


# ---------------------------------------------------------------------------
# LLM plumbing (transport-free)
# ---------------------------------------------------------------------------

def test_strip_code_fences():
    assert llm.strip_code_fences("plain") == "plain"
    assert llm.strip_code_fences("```cpp\nint x;\n```") == "int x;"
    assert llm.strip_code_fences("```\nint x;\n```\n") == "int x;"


def test_generate_with_retries_accepts_any_callable():
    calls = []

    def gen(attempt):
        calls.append(attempt)
        return f"candidate-{attempt}"

    out = llm.generate_with_retries(gen, lambda c: c.endswith("-2"), max_attempts=5)
    assert out == "candidate-2"
    assert calls == [0, 1, 2]            # stopped at first accepted

    # none accepted: returns the last candidate
    out = llm.generate_with_retries(gen, lambda c: False, max_attempts=2)
    assert out.startswith("candidate-")


def test_collect_target_headers_include_closure(tmp_path):
    d = tmp_path / "hdrs"
    d.mkdir()
    (d / "top.h").write_text('#include "used.h"\n', encoding="utf-8")
    (d / "used.h").write_text("int u;\n", encoding="utf-8")
    (d / "unused.h").write_text("int n;\n", encoding="utf-8")
    driver_text = '#include "top.h"\n#include <cmath>\nint main(){}\n'

    closure, others = llm.collect_target_headers(d, driver_text)
    assert [p.name for p in closure] == ["top.h", "used.h"]
    assert [p.name for p in others] == ["unused.h"]


def test_embed_file_caps_length(tmp_path):
    f = tmp_path / "big.h"
    f.write_text("x" * (llm.HEADER_EMBED_CAP + 100), encoding="utf-8")
    section = llm.embed_file(f, "big.h")
    assert "[truncated 100 chars]" in section
    assert section.startswith("### `big.h`")
