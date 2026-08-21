"""Type-boundary annotations from compiler diagnostics (``tracked-boundary-patch``).

Some target libraries contain, in their OWN source, int<->tracked crossings
that a free-function shim cannot bridge (the instrumented scalar has an
EXPLICIT scalar ctor and no ``operator int``/``operator bool``).  Rather than
hunt for these heuristically, let the compiler find them: build the shimmed
target once, and map the resulting int<->tracked diagnostics to source
annotations.  The compiler is a perfect, reproducible detector -- it names the
exact site and crossing type -- so the mapping is pure Python (no LLM, no
shim-cache coupling).  Three recognized crossing patterns, each a mechanical
rewrite:

  (a) tracked value assigned to an int/bool lvalue
        -> wrap the RHS: ``X = static_cast<int>((RHS).value())``
  (b) an int/bool bound where a tracked scalar (const&) is expected
        -> wrap the argument: ``<TrackedType>(arg)``
  (c) a tracked value compared to an integer/boolean literal
        -> ``.value()`` on the tracked operand

An int<->tracked-flavored diagnostic that fits none of the three is a hard
failure (``C8_UNCLASSIFIED_ERROR``) surfaced for human review -- the same
ambiguity-surfacing discipline as the interop ruleset's UNCLASSIFIED escape
hatch (tracked_tools/interop/ruleset.txt, Rule 9).

**Parameterized on the target scalar type name** (``tracked_type_name``, e.g.
``tracked::Tracked``): the type-independent diagnostic shapes (the ``error:``
line shape, the ``operator==`` "no match" form, the argument-note, the
assignment-``=`` finder) are compiled once at module load; the three
type-dependent patterns are built per call from the supplied name
(``re.escape``d, so ``::`` passes through unchanged).

Diagnostics dialect: gcc (curly or straight quotes).  clang's diagnostic
layout differs and is not recognized in v1 -- run the detection build with
gcc (documented limitation, ratified decision 5).
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

# gcc renders identifiers in ‘curly’ quotes; accept those and straight quotes.
_Q = "[‘’'\"`]"

# --- type-independent diagnostic shapes (compiled once) --------------------
_C8_ERR_RE = re.compile(r"^(.*?):(\d+):(\d+): error: (.*)$")
_C8_NOEQ_RE = re.compile(
    r"no match for " + _Q + r"operator(==|!=)" + _Q
    + r" \(operand types are " + _Q + r"(.*?)" + _Q + r" and " + _Q + r"(.*?)" + _Q + r"\)"
)
_C8_ARGNOTE_RE = re.compile(r"in passing argument (\d+) of\b.*?([A-Za-z_][\w:]*)\s*\(")
# an assignment '=' that is not ==, <=, >=, !=, +=, etc.
_C8_ASSIGN_EQ_RE = re.compile(r"(?<![=<>!+\-*/%&|^~])=(?![=])")


class _TypePatterns:
    """The three type-dependent diagnostic regexes, built from a type name."""

    __slots__ = ("tracked", "assign", "refbind", "prefix")

    def __init__(self, tracked_type_name: str) -> None:
        esc = re.escape(tracked_type_name)
        self.prefix = tracked_type_name + "<"
        self.tracked = re.compile(esc + r"<")
        self.assign = re.compile(
            r"cannot convert " + _Q + esc + r"<.*?>" + _Q
            + r" to " + _Q + r"(?:int|bool|unsigned int|long|size_t)" + _Q
            + r" in (?:assignment|initialization)"
        )
        self.refbind = re.compile(
            r"invalid initialization of reference of type " + _Q
            + r"const (" + esc + r"<.*?>)\s*&" + _Q
            + r" from expression of type " + _Q
            + r"(?:int|bool|unsigned int|long|size_t)" + _Q
        )


def derive_c8_patch(
    compile_stderr: str,
    headers_dir,
    repo_root,
    *,
    tracked_type_name: str,
) -> str | None:
    """Map int<->tracked compile diagnostics to a git-apply-able library patch.

    Parses ``compile_stderr`` for the three recognized int<->tracked crossing
    patterns (C8 a/b/c) at sites INSIDE ``headers_dir``, turns each into an exact
    source edit, and synthesizes a unified diff via :func:`synthesize_patch`
    (``a/``,``b/`` labels relative to ``repo_root``).  ``tracked_type_name`` is
    the target's concrete instrumented scalar spelling (e.g. ``tracked::Tracked``).

    Returns ``None`` when the diagnostics contain no int<->tracked crossing at
    all (a genuine, non-C8 build failure).  Raises ``RuntimeError`` prefixed
    ``C8_UNCLASSIFIED_ERROR`` for an int<->tracked-flavored diagnostic that fits
    none of the three patterns — surfacing it for review rather than guessing.
    """
    headers_dir = Path(headers_dir).resolve()
    repo_root = Path(repo_root).resolve()
    pats = _TypePatterns(tracked_type_name)
    records = _parse_c8_errors(compile_stderr, headers_dir, pats)
    if not records:
        return None
    return synthesize_patch(records, headers_dir, repo_root)


def _parse_c8_errors(stderr: str, headers_dir: Path, pats: _TypePatterns) -> list[dict]:
    """Classify int<->tracked diagnostics into deduplicated C8 edit records."""
    records: list[dict] = []
    unclassified: list[str] = []
    seen: set[tuple] = set()

    for block in _iter_error_blocks(stderr):
        try:
            target = Path(block["file"]).resolve()
        except (OSError, ValueError):
            continue
        # Only crossings inside the library we may patch are C8's concern.
        if not is_within(target, headers_dir):
            continue

        rec = None
        if pats.assign.search(block["msg"]):
            rec = _map_assign(block, headers_dir)
        elif pats.refbind.search(block["msg"]):
            rec = _map_refbind(block, headers_dir, pats)
        elif _C8_NOEQ_RE.search(block["msg"]):
            rec = _map_noeq(block, headers_dir, pats)
        elif _is_int_tracked_flavored(block["msg"], pats):
            unclassified.append(f"{target}:{block['line']}: {block['msg']}")
            continue
        else:
            continue

        if rec is None:
            unclassified.append(f"{target}:{block['line']}: {block['msg']}")
            continue
        key = (rec["file"], rec["original"], rec["replacement"])
        if key not in seen:
            seen.add(key)
            records.append(rec)

    if unclassified:
        raise RuntimeError(
            "C8_UNCLASSIFIED_ERROR: int<->tracked crossing(s) matched no known "
            "pattern (a)/(b)/(c) — human review needed:\n  "
            + "\n  ".join(unclassified)
        )
    return records


def _iter_error_blocks(stderr: str):
    """Yield one dict per compiler ``error:`` diagnostic.

    Each block carries the error ``file``/``line``/``col``/``msg`` plus the
    diagnostic's own source line, caret ruler, and any following ``note:`` lines
    (the "in passing argument N of ..." note drives pattern (b)).
    """
    lines = stderr.splitlines()
    i = 0
    while i < len(lines):
        m = _C8_ERR_RE.match(lines[i])
        if not m:
            i += 1
            continue
        block = {
            "file": m.group(1), "line": int(m.group(2)), "col": int(m.group(3)),
            "msg": m.group(4), "src": None, "caret": None, "notes": [],
        }
        j = i + 1
        while j < len(lines) and not _C8_ERR_RE.match(lines[j]):
            lj = lines[j]
            if re.match(r"^\s*\d+\s*\|", lj) and block["src"] is None:
                block["src"] = lj
            elif "|" in lj and ("^" in lj or "~" in lj) and block["src"] is not None \
                    and block["caret"] is None:
                block["caret"] = lj
            if "note:" in lj:
                block["notes"].append(lj)
            j += 1
        yield block
        i = j


def _caret_span(block: dict) -> str | None:
    """Extract the exact source substring gcc's caret ruler (``~~~^~~~``) marks."""
    src, caret = block.get("src"), block.get("caret")
    if not src or not caret or "|" not in src or "|" not in caret:
        return None
    s = src[src.index("|") + 1:]
    c = caret[caret.index("|") + 1:]
    idx = [k for k, ch in enumerate(c) if ch in "~^"]
    if not idx:
        return None
    return s[idx[0]:idx[-1] + 1]


def _file_line(headers_dir: Path, block: dict) -> str:
    """Read the exact source line the diagnostic points at, from the file."""
    target = Path(block["file"]).resolve()
    text = target.read_text(encoding="utf-8").splitlines()
    return text[block["line"] - 1]


def _rel_in(headers_dir: Path, block: dict) -> str:
    return Path(block["file"]).resolve().relative_to(headers_dir).as_posix()


def _map_assign(block: dict, headers_dir: Path) -> dict | None:
    """(a) `LHS = <tracked RHS>` -> `LHS = static_cast<int>((RHS).value())`."""
    flagged = _caret_span(block)
    if flagged is None:
        return None
    eqm = _C8_ASSIGN_EQ_RE.search(flagged)
    if eqm is None:
        return None
    lhs, rhs = flagged[:eqm.start()], flagged[eqm.end():]
    replacement = f"{lhs}= static_cast<int>(({rhs.strip()}).value())"
    return {
        "file": _rel_in(headers_dir, block),
        "original": flagged,
        "replacement": replacement,
        "rule": "C8(a) tracked->int assignment",
    }


def _map_refbind(block: dict, headers_dir: Path, pats: _TypePatterns) -> dict | None:
    """(b) int arg bound to `const Tracked&` -> wrap arg in the tracked ctor."""
    mtype = pats.refbind.search(block["msg"])
    tracked_type = mtype.group(1)
    note = next((n for n in block["notes"] if "in passing argument" in n), "")
    ma = _C8_ARGNOTE_RE.search(note)
    if ma is None:
        return None
    argn, callee = int(ma.group(1)), ma.group(2)
    line = _file_line(headers_dir, block)
    span = extract_call_arg(line, callee, argn)
    if span is None:
        return None
    arg_text, start, end = span
    # Preserve the argument's leading whitespace (', ' after the comma) so the
    # rewritten call reads naturally; only the token itself is wrapped.
    lead = arg_text[: len(arg_text) - len(arg_text.lstrip())]
    new_arg = f"{lead}{tracked_type}({arg_text.strip()})"
    replacement = line[:start] + new_arg + line[end:]
    return {
        "file": _rel_in(headers_dir, block),
        "original": line,
        "replacement": replacement,
        "rule": f"C8(b) int->tracked ref bind (arg {argn})",
    }


def _map_noeq(block: dict, headers_dir: Path, pats: _TypePatterns) -> dict | None:
    """(c) `tracked <op> intlit` -> `.value()` on the tracked operand."""
    m = _C8_NOEQ_RE.search(block["msg"])
    op, left_type, right_type = m.group(1), m.group(2), m.group(3)
    flagged = _caret_span(block)
    if flagged is None or op not in flagged:
        return None
    lhs, _, rhs = flagged.partition(op)
    left_tracked = pats.prefix in left_type
    right_tracked = pats.prefix in right_type
    if left_tracked and not right_tracked:
        replacement = f"({lhs.strip()}).value() {op} {rhs.strip()}"
    elif right_tracked and not left_tracked:
        replacement = f"{lhs.strip()} {op} ({rhs.strip()}).value()"
    else:
        return None
    return {
        "file": _rel_in(headers_dir, block),
        "original": flagged,
        "replacement": replacement,
        "rule": f"C8(c) tracked {op} int comparison",
    }


def extract_call_arg(line: str, callee: str, n: int):
    """Return (text, start, end) of the n-th (1-based) top-level call argument.

    Locates ``callee``'s argument list on ``line`` (skipping a balanced template
    ``<...>``), then splits the parenthesized list on top-level commas — angle
    brackets and nested brackets are respected so template commas don't split.
    """
    pos = line.find(callee)
    if pos < 0:
        return None
    i = pos + len(callee)
    ang = 0
    while i < len(line):
        ch = line[i]
        if ch == "<":
            ang += 1
        elif ch == ">":
            ang -= 1
        elif ch == "(" and ang <= 0:
            break
        i += 1
    if i >= len(line) or line[i] != "(":
        return None
    open_paren = i
    depth = 0
    j = open_paren
    while j < len(line):
        if line[j] == "(":
            depth += 1
        elif line[j] == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    if j >= len(line):
        return None
    inner_start = open_paren + 1
    inner = line[inner_start:j]
    args = split_top_level(inner)
    if n < 1 or n > len(args):
        return None
    text, off = args[n - 1]
    start = inner_start + off
    return text, start, start + len(text)


def split_top_level(s: str):
    """Split ``s`` on top-level commas; return [(arg_text, offset), ...]."""
    out = []
    depth = 0
    ang = 0
    cur_start = 0
    for k, ch in enumerate(s):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "<":
            ang += 1
        elif ch == ">":
            ang -= 1
        elif ch == "," and depth == 0 and ang <= 0:
            out.append((s[cur_start:k], cur_start))
            cur_start = k + 1
    out.append((s[cur_start:], cur_start))
    return out


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_int_tracked_flavored(msg: str, pats: _TypePatterns) -> bool:
    """True if the diagnostic looks like an int<->tracked crossing at all."""
    if not pats.tracked.search(msg):
        return False
    return any(tok in msg for tok in ("int", "bool", "unsigned", "long", "size_t"))


def synthesize_patch(records: list[dict], headers_dir: Path, repo_root: Path) -> str | None:
    """Turn edit records into a deterministic, git-apply-able unified diff.

    Each record names a library header (relative to ``headers_dir``), an exact
    ``original`` substring, and its ``replacement``.  ``original`` MUST occur
    EXACTLY ONCE in the clean file — 0 or >1 is a hard failure (surfacing an
    ambiguous edit rather than a silent first-match) — and the diff is produced
    by :func:`difflib.unified_diff` with ``a/``,``b/`` repo-relative labels so
    ``git apply -p1`` from the repo root applies it.  Returns the combined diff,
    or ``None`` when there are no records.
    """
    if not records:
        return None
    edits_by_file: dict[str, list[tuple[str, str, str]]] = {}
    for rec in records:
        edits_by_file.setdefault(rec["file"], []).append(
            (rec["original"], rec["replacement"], rec.get("rule", ""))
        )

    diff_chunks: list[str] = []
    for relfile, edits in sorted(edits_by_file.items()):
        target = (headers_dir / relfile).resolve()
        if not target.is_file():
            raise RuntimeError(f"integrator_base C8: patch target not found: {relfile}")
        original_text = target.read_text(encoding="utf-8")
        patched_text = original_text
        for original, replacement, rule in edits:
            clean_count = original_text.count(original)
            if clean_count != 1:
                raise RuntimeError(
                    f"integrator_base C8: 'original' must occur exactly once in "
                    f"{relfile} (found {clean_count}) [{rule}]: {original!r}"
                )
            if patched_text.count(original) < 1:
                raise RuntimeError(
                    f"integrator_base C8: 'original' already consumed by an "
                    f"earlier edit in {relfile} [{rule}]: {original!r}"
                )
            patched_text = patched_text.replace(original, replacement, 1)
        rel = target.relative_to(repo_root).as_posix()
        diff = difflib.unified_diff(
            original_text.splitlines(keepends=True),
            patched_text.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}",
        )
        diff_chunks.append("".join(diff))

    combined = "".join(diff_chunks)
    return combined if combined.strip() else None


# ---------------------------------------------------------------------------
# CLI (tracked-boundary-patch)
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        description="Map int<->tracked gcc diagnostics to a library patch.")
    ap.add_argument("--stderr", required=True,
                    help="file with the compiler stderr, or '-' for stdin")
    ap.add_argument("--headers", required=True, help="target header tree")
    ap.add_argument("--repo-root", default=".", help="root for a/ b/ patch labels")
    ap.add_argument("--type", required=True, dest="type_name",
                    help="instrumented scalar spelling, e.g. tracked::Tracked")
    ap.add_argument("-o", "--out", required=True, help="output .patch path")
    args = ap.parse_args(argv)

    if args.stderr == "-":
        text = sys.stdin.read()
    else:
        text = Path(args.stderr).read_text(encoding="utf-8")

    patch = derive_c8_patch(text, Path(args.headers), Path(args.repo_root),
                            tracked_type_name=args.type_name)
    if patch is None:
        print("tracked-boundary-patch: no int<->tracked crossings found "
              "(genuine non-C8 build failure?)")
        return 2
    Path(args.out).write_text(patch, encoding="utf-8")
    print(f"tracked-boundary-patch: wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
