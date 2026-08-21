"""Per-statement ``line=`` scope injection for operator-heavy tracked code
(``tracked-line-inject``).

Whole-app tracked journals attribute every op to the scope suffix baked into
its id (``tracked::detail::current_scope_suffix``).  C++ operator ops carry no
source location — ``operator+``/``operator*`` etc. cannot take a trailing
``SourceLocation`` — so the ONLY way to attribute the operator arithmetic that
dominates a numeric kernel to a source line is a ``line=<basename>:<N>`` scope
pushed *around the statement*.

This module walks the libclang AST of a driver translation unit, finds every
value-producing statement inside a target header tree, and emits a
``git apply``-able patch that wraps each one with a ``line=`` scope:

* **declarations** →
  ``tracked::push_scope("line=f.h:10"); <decl;> tracked::pop_scope();``
  A lexical-block RAII wrap would scope the declared name out of the enclosing
  block, so declarations use the free push/pop pair (see ``tracked.hpp``).
* **everything else** (assignments, expression statements, returns, …) →
  ``{ tracked::scope <var>("line=f.h:10"); <stmt;> }``
  A single brace-delimited statement, valid even as a brace-less ``if``/``for``
  branch, and (unlike push/pop) exception-safe on the way out.

Design invariants:

* **Value-neutral.** A scope touches only the id-suffix string, never the
  numeric path, so an instrumented build is bit-identical to an uninstrumented
  one.
* **Basename labels.** ``line=`` values are header basenames (``B2m.h:84``),
  never paths: the library's scope grammar (docs/SCHEMA.md) forbids ``/`` and
  ``=`` in scope values (the scope stack joins with ``/`` and splits key from
  value on ``=``), and ``tracked::push_scope`` validates on push.  A basename
  containing either character is rejected at generation time.  The tree must
  therefore have **no basename collisions** among instrumented headers
  (checked: exactly-one-match resolution below).
* **Structural detection.** Numeric kernels are typically *templates*;
  dependent types mean libclang cannot know they instantiate to ``Tracked``.
  A statement is therefore instrumented when its AST subtree *structurally*
  contains an operator/call node — not by any semantic "is-tracked" test and
  not by header/function name.  Over-instrumenting a non-tracked statement is
  harmless: it yields an empty ``line=`` scope with no ops under it.

Parameterization (all library-consumer seams): extra include dirs and
preprocessor defines for the libclang parse, and the RAII scope variable name
spliced into non-declaration wraps.  NOTE: the regeneration cache key
(:func:`cache_key`) covers the header tree, the transform version, and the
composed C8 patch — NOT these parameters; changing them requires ``--force``.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import subprocess
import sys
from pathlib import Path

# Bump when the injection transform changes (statement selection, wrap syntax,
# semicolon handling).  Folded into the cache key so a transform change forces
# regeneration even when the header tree and C8 patch are unchanged.
TRANSFORM_VERSION = 1

# Spliced into non-declaration wraps; override for consumers with their own
# naming conventions (must be a valid, unused C++ identifier at every site).
DEFAULT_SCOPE_VAR = "_tracked_line_scope"

_HEADER_SUFFIXES = {".h", ".hpp", ".hh", ".hxx", ".ipp", ".inc", ".cuh", ".tcc"}

# libclang is a dev/build-time dependency (the self-contained wheel bundles its
# own native library); imported lazily so importing this module never hard-fails.
_CINDEX = None


def _cindex():
    global _CINDEX
    if _CINDEX is None:
        import clang.cindex as cindex  # noqa: PLC0415
        _CINDEX = cindex
    return _CINDEX


# ---------------------------------------------------------------------------
# statement classification (cursor kinds)
# ---------------------------------------------------------------------------
#
# Names are resolved at call time from the cindex enum so a missing kind on an
# older/newer libclang doesn't crash import.

def _kinds(*names):
    C = _cindex()
    out = set()
    for n in names:
        k = getattr(C.CursorKind, n, None)
        if k is not None:
            out.add(k)
    return out


def _op_kinds():
    """Kinds whose presence marks a statement as value-producing arithmetic."""
    return _kinds(
        "BINARY_OPERATOR", "UNARY_OPERATOR", "COMPOUND_ASSIGNMENT_OPERATOR",
        "CALL_EXPR", "CXX_OPERATOR_CALL_EXPR", "CONDITIONAL_OPERATOR",
    )


def _decl_kinds():
    return _kinds("DECL_STMT")


def _control_kinds():
    """Statements we never wrap — we recurse into their nested blocks instead."""
    return _kinds(
        "IF_STMT", "FOR_STMT", "CXX_FOR_RANGE_STMT", "WHILE_STMT", "DO_STMT",
        "SWITCH_STMT", "COMPOUND_STMT", "CASE_STMT", "DEFAULT_STMT",
        "LABEL_STMT", "NULL_STMT",
    )


# ---------------------------------------------------------------------------
# compiler include search paths (for a tolerant libclang parse)
# ---------------------------------------------------------------------------

def gcc_search_dirs(cxx: str = "g++") -> list[str]:
    """The system ``#include <...>`` search list of the active compiler.

    libclang's wheel bundles ``libclang.so`` but NOT clang's builtin-header
    resource dir, so ``stddef.h`` (a compiler builtin) is unresolved out of the
    box and parsing fatally stops.  Handing libclang the active compiler's own
    search list (compiler builtins + stdlib + system) fixes this and keeps the
    parse consistent with the compiler that actually builds the kernel.
    Extracted at runtime (not hardcoded) so it tracks the environment.
    """
    try:
        proc = subprocess.run(
            [cxx, "-x", "c++", "-E", "-v", "-"],
            input="", capture_output=True, text=True,
        )
    except FileNotFoundError:
        # No compiler on PATH (e.g. a unit test parsing a self-contained snippet
        # that includes no system headers).  The caller gets clang's defaults.
        return []
    out = proc.stderr + proc.stdout
    dirs: list[str] = []
    grab = False
    for ln in out.splitlines():
        if "#include <...> search starts here:" in ln:
            grab = True
            continue
        if "End of search list." in ln:
            break
        if grab:
            d = ln.strip()
            # gcc prints "(framework directory)" suffixes on darwin; keep plain dirs
            if d and Path(d.split(" (")[0]).is_dir():
                dirs.append(d.split(" (")[0])
    return dirs


def _parse_args(headers_dir: Path, tracked_include: Path, driver_dir: Path,
                cxx_standard: int,
                system_include_dirs: list[str] | None = None,
                extra_include_dirs: list = (),
                defines: list = ()) -> list[str]:
    args = [f"-std=c++{cxx_standard}", "-x", "c++", "-ferror-limit=0",
            "-nostdinc++"]
    dirs = system_include_dirs if system_include_dirs is not None else gcc_search_dirs()
    for d in dirs:
        args += ["-isystem", d]
    args += [
        f"-I{tracked_include}",
        f"-I{headers_dir}",
        f"-I{driver_dir}",
    ]
    for d in extra_include_dirs:
        args.append(f"-I{d}")
    for d in defines:
        args.append(f"-D{d}")
    return args


# ---------------------------------------------------------------------------
# AST walk
# ---------------------------------------------------------------------------

def _has_op(node, op_kinds) -> bool:
    """True if the cursor's subtree contains any value-producing op/call node."""
    if node.kind in op_kinds:
        return True
    return any(_has_op(c, op_kinds) for c in node.get_children())


class _Site:
    """One statement to wrap: byte span (incl. terminating ``;``), kind, label."""

    __slots__ = ("basename", "line", "start", "end", "is_decl")

    def __init__(self, basename, line, start, end, is_decl):
        self.basename = basename
        self.line = line
        self.start = start
        self.end = end
        self.is_decl = is_decl


def _stmt_span(node, src: bytes, decl_kinds) -> tuple[int, int]:
    """[start, end) byte span of the full statement, including its ``;``.

    ``DECL_STMT`` extents already include the terminating semicolon; expression
    and terminator statements end at the last expression token, so scan forward
    to the next ``;`` (there is none inside a full-expression statement).
    """
    start = node.extent.start.offset
    end = node.extent.end.offset
    if node.kind in decl_kinds:
        return start, end
    n = len(src)
    i = end
    while i < n and src[i:i + 1] != b";":
        i += 1
    if i < n:
        i += 1  # include the ';'
    return start, i


def _collect_sites(tu, target_basenames: set[str] | None,
                   headers_dir: Path) -> dict[str, list[_Site]]:
    """Walk every block, recording each value-producing leaf statement.

    We wrap only statements that are *direct children of a compound statement*
    (a real ``{ }`` block).  Nested blocks are reached by recursion (a compound
    body of an ``if``/``for``/… is itself a compound statement), so this covers
    top-level and nested block statements alike.  Brace-less single-statement
    branches are intentionally left un-wrapped: they are rare, add parsing risk,
    and their ops simply fall to the enclosing region.
    """
    C = _cindex()
    op_kinds = _op_kinds()
    decl_kinds = _decl_kinds()
    control_kinds = _control_kinds()
    headers_dir = headers_dir.resolve()

    sites: dict[str, list[_Site]] = {}
    src_cache: dict[str, bytes] = {}

    def in_target(node) -> bool:
        f = node.location.file
        if f is None:
            return False
        p = Path(f.name).resolve()
        try:
            p.relative_to(headers_dir)
        except ValueError:
            return False
        if target_basenames is not None and p.name not in target_basenames:
            return False
        return True

    def src_of(path: str) -> bytes:
        if path not in src_cache:
            src_cache[path] = Path(path).read_bytes()
        return src_cache[path]

    def visit(node):
        if node.kind == C.CursorKind.COMPOUND_STMT and in_target(node):
            for child in node.get_children():
                if (child.kind not in control_kinds
                        and in_target(child)
                        and _has_op(child, op_kinds)):
                    path = child.location.file.name
                    start, end = _stmt_span(child, src_of(path), decl_kinds)
                    base = Path(path).name
                    if "=" in base or "/" in base:
                        raise RuntimeError(
                            f"line_injector: header basename {base!r} contains "
                            "'=' or '/', which the tracked scope grammar "
                            "forbids in line= values (docs/SCHEMA.md)"
                        )
                    sites.setdefault(base, []).append(
                        _Site(base, child.extent.start.line, start, end,
                              child.kind in decl_kinds)
                    )
        for child in node.get_children():
            visit(child)

    visit(tu.cursor)
    return sites


# ---------------------------------------------------------------------------
# text transform + patch synthesis
# ---------------------------------------------------------------------------

def _instrument_text(src: bytes, sites: list[_Site],
                     scope_var_name: str = DEFAULT_SCOPE_VAR) -> bytes:
    """Splice line= wraps into one file's bytes, applied end-to-start."""
    out = src
    for s in sorted(sites, key=lambda s: s.start, reverse=True):
        stmt = out[s.start:s.end]
        label = f"line={s.basename}:{s.line}".encode("utf-8")
        if s.is_decl:
            wrapped = (b'tracked::push_scope("' + label + b'"); '
                       + stmt + b' tracked::pop_scope();')
        else:
            wrapped = (b'{ tracked::scope ' + scope_var_name.encode("utf-8")
                       + b'("' + label + b'"); ' + stmt + b' }')
        out = out[:s.start] + wrapped + out[s.end:]
    return out


def build_line_patch(
    driver_source: Path,
    headers_dir: Path,
    tracked_include: Path,
    repo_root: Path,
    target_basenames: set[str] | None = None,
    cxx_standard: int = 17,
    system_include_dirs: list[str] | None = None,
    extra_include_dirs: list = (),
    defines: list = (),
    scope_var_name: str = DEFAULT_SCOPE_VAR,
) -> tuple[str | None, dict]:
    """Parse ``driver_source`` and emit a ``line=`` injection patch.

    Returns ``(patch_text_or_None, stats)``.  ``patch_text`` is a combined
    ``git apply -p1`` unified diff (``a/`` ``b/`` repo-relative labels) covering
    every target header with at least one instrumented statement; ``None`` when
    nothing was instrumented.  ``stats`` reports per-file site counts for the
    caller to sanity-check coverage.

    The parse reads the on-disk header tree, so callers that want the patch to
    compose with an int↔tracked (C8) patch should apply that patch first and
    generate this one against the patched tree (or use :func:`generate`, which
    automates the apply/reset).
    """
    C = _cindex()
    driver_source = Path(driver_source).resolve()
    headers_dir = Path(headers_dir).resolve()
    repo_root = Path(repo_root).resolve()

    args = _parse_args(headers_dir, Path(tracked_include).resolve(),
                       driver_source.parent, cxx_standard,
                       system_include_dirs=system_include_dirs,
                       extra_include_dirs=extra_include_dirs,
                       defines=defines)
    index = C.Index.create()
    tu = index.parse(str(driver_source), args=args)

    fatal = [d for d in tu.diagnostics
             if d.severity >= C.Diagnostic.Fatal and d.location.file is None]
    # A fatal with no file is the "too many errors" stop; per-header fatals in
    # framework internals are tolerated (they don't affect the target-header
    # statement extents), but a global stop means bodies may be truncated.
    if fatal:
        raise RuntimeError(
            "line_injector: libclang stopped parsing (global fatal): "
            + "; ".join(d.spelling for d in fatal)
        )

    sites = _collect_sites(tu, target_basenames, headers_dir)
    stats = {base: len(lst) for base, lst in sorted(sites.items())}
    if not sites:
        return None, stats

    diff_chunks: list[str] = []
    for base in sorted(sites):
        # resolve the real path for this basename under headers_dir
        matches = [p for p in headers_dir.rglob(base) if p.is_file()]
        if len(matches) != 1:
            raise RuntimeError(
                f"line_injector: expected exactly one {base} under "
                f"{headers_dir} (found {len(matches)})"
            )
        target = matches[0]
        original = target.read_bytes()
        patched = _instrument_text(original, sites[base], scope_var_name)
        rel = target.relative_to(repo_root).as_posix()
        diff = difflib.unified_diff(
            original.decode("utf-8").splitlines(keepends=True),
            patched.decode("utf-8").splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}",
        )
        diff_chunks.append("".join(diff))

    combined = "".join(diff_chunks)
    return (combined if combined.strip() else None), stats


# ---------------------------------------------------------------------------
# caching
# ---------------------------------------------------------------------------

def cache_key(headers_dir: Path, c8_patch: Path | None) -> str:
    """Staleness key: header-tree bytes + transform version + C8 patch bytes.

    The cache hits only when the target headers, the injection transform, and
    the (optional) C8 patch the line patch composes with are all unchanged.
    Stored in a ``<patch>.hash`` sidecar so the ``.patch`` file itself stays a
    clean ``git apply`` input.  NOTE: parameterization (scope var name, include
    dirs, defines) is deliberately NOT in the key — change those with --force.
    """
    h = hashlib.sha256()
    headers_dir = Path(headers_dir).resolve()
    for path in sorted(p for p in headers_dir.rglob("*")
                       if p.is_file() and p.suffix.lower() in _HEADER_SUFFIXES):
        h.update(path.relative_to(headers_dir).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    h.update(f"transform={TRANSFORM_VERSION}\0".encode("utf-8"))
    if c8_patch is not None and Path(c8_patch).is_file():
        h.update(Path(c8_patch).read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# git apply/reset around generation (so the patch composes with the C8 patch)
# ---------------------------------------------------------------------------

def _git(repo_root: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo_root),
                          capture_output=True, text=True)


def generate(
    driver_source: Path,
    headers_dir: Path,
    tracked_include: Path,
    repo_root: Path,
    out_patch: Path,
    c8_patch: Path | None = None,
    target_basenames: set[str] | None = None,
    cxx_standard: int = 17,
    extra_include_dirs: list = (),
    defines: list = (),
    scope_var_name: str = DEFAULT_SCOPE_VAR,
    force: bool = False,
) -> dict:
    """Generate ``out_patch`` (and its ``.hash`` sidecar), composing with C8.

    When ``c8_patch`` is given it is ``git apply``-ed to the header tree before
    parsing and reset afterward, so the line patch is generated against — and
    therefore applies cleanly on top of — the C8-patched tree.  Returns a dict
    with ``{"cached", "stats", "key", "patch"}``.
    """
    repo_root = Path(repo_root).resolve()
    out_patch = Path(out_patch)
    hash_file = out_patch.with_suffix(out_patch.suffix + ".hash")

    key = cache_key(headers_dir, c8_patch)
    if (not force and out_patch.is_file() and hash_file.is_file()
            and hash_file.read_text(encoding="utf-8").strip() == key):
        return {"cached": True, "stats": None, "key": key, "patch": out_patch}

    applied_c8 = False
    try:
        if c8_patch is not None:
            r = _git(repo_root, "apply", str(Path(c8_patch).resolve()))
            if r.returncode != 0:
                raise RuntimeError(f"line_injector: C8 apply failed:\n{r.stderr}")
            applied_c8 = True
        patch, stats = build_line_patch(
            driver_source=driver_source, headers_dir=headers_dir,
            tracked_include=tracked_include, repo_root=repo_root,
            target_basenames=target_basenames, cxx_standard=cxx_standard,
            extra_include_dirs=extra_include_dirs, defines=defines,
            scope_var_name=scope_var_name,
        )
    finally:
        if applied_c8:
            _git(repo_root, "checkout", "--", str(Path(headers_dir).resolve()))

    if patch is None:
        raise RuntimeError("line_injector: no statements instrumented")
    out_patch.write_text(patch, encoding="utf-8")
    hash_file.write_text(key + "\n", encoding="utf-8")
    return {"cached": False, "stats": stats, "key": key, "patch": out_patch}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Generate a per-statement line= scope injection patch.")
    ap.add_argument("--driver", required=True, help="driver .cpp translation unit")
    ap.add_argument("--headers", required=True, help="target header tree (patch-only)")
    ap.add_argument("--tracked-include", required=True, help="tracked include/ dir")
    ap.add_argument("--repo-root", default=".", help="repo root for a/ b/ patch labels")
    ap.add_argument("--out", required=True, help="output .patch path")
    ap.add_argument("--include", action="append", default=[], metavar="DIR",
                    help="extra -I dir for the libclang parse (repeatable)")
    ap.add_argument("--define", action="append", default=[], metavar="MACRO[=V]",
                    help="extra -D for the libclang parse (repeatable)")
    ap.add_argument("--scope-var", default=DEFAULT_SCOPE_VAR,
                    help=f"RAII scope variable name (default {DEFAULT_SCOPE_VAR})")
    ap.add_argument("--c8-patch", default=None,
                    help="int<->tracked patch applied before parsing (and reset after)")
    ap.add_argument("--cxx-standard", type=int, default=17)
    ap.add_argument("--force", action="store_true", help="ignore the cache")
    args = ap.parse_args(argv)

    res = generate(
        driver_source=Path(args.driver), headers_dir=Path(args.headers),
        tracked_include=Path(args.tracked_include), repo_root=Path(args.repo_root),
        out_patch=Path(args.out),
        c8_patch=Path(args.c8_patch) if args.c8_patch else None,
        cxx_standard=args.cxx_standard,
        extra_include_dirs=args.include, defines=args.define,
        scope_var_name=args.scope_var, force=args.force,
    )
    if res["cached"]:
        print(f"line_injector: up to date (cache hit): {args.out}")
    else:
        total = sum(res["stats"].values())
        print(f"line_injector: wrote {args.out} ({total} sites): {res['stats']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
