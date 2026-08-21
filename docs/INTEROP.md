# Interop conventions (shims, barriers, and source locations)

Library-side conventions that interop shims — hand-written or generated with
`tracked_tools/interop/` — rely on. The generation ruleset itself ships as
[`tools/tracked_tools/interop/ruleset.txt`](../tools/tracked_tools/interop/ruleset.txt)
(Rules 1–9 + C1–C7; C1–C7 follow from the Tracked API surface and hold for any
target library).

## The single-default-argument rule

Every named Tracked op takes exactly **one trailing defaulted parameter**:
`SourceLocation loc = {}`. Keep that shape in every shim you write:

```cpp
template <class T>
tracked::Tracked<T> my_shim(const tracked::Tracked<T>& x,
                            tracked::SourceLocation loc = {}) {
    ...
}
```

Why exactly one, and why last: a call site can then always add attribution by
appending `TRACKED_HERE` — `my_shim(x, TRACKED_HERE)` — without naming or
re-defaulting any other parameter, and an unattributed call stays valid. Two
defaulted parameters would force call sites to fill the first to reach the
second; a non-trailing `loc` breaks drop-in compatibility with the wrapped
signature.

## TRACKED_HERE forwarding

`TRACKED_HERE` (`journal.hpp`) captures `__FILE__, __func__, __LINE__` **at the
point it is written**. A wrapper that wants its *call site* attributed must
therefore accept a `loc` and forward it — never bake `TRACKED_HERE` into the
wrapper body (that attributes every call to the wrapper's own source line):

```cpp
template <class T>
tracked::Tracked<T> shim_sqrt(const tracked::Tracked<T>& x,
                              tracked::SourceLocation loc = {}) {
    return tracked::sqrt(x, loc);          // forward, don't re-capture
}
```

## Opaque barriers with attribution

When a shim crosses a non-Tracked boundary (framework math, vendor kernels),
compute on raw `.value()`s and re-wrap with `tracked::opaque_at(...)`,
forwarding the tracked inputs so error and provenance flow through the black
box — and forwarding `loc` for attribution:

```cpp
template <class T>
tracked::Tracked<T> shim_ext_log(const tracked::Tracked<T>& x,
                                 tracked::SourceLocation loc = {}) {
    T raw = ext::log(x.value());
    return tracked::opaque_at("ext::log", raw, loc, x);
}
```

## Shim caching (SOURCE_HASH)

Generated shims carry a `// SOURCE_HASH: <hex>` line —
`sha256(target-header-tree ⊕ ruleset-text)` (`tracked_tools.interop.cache`).
A cached shim is reused only when **both** the target headers and the
generating ruleset are unchanged; `PENDING` never counts as a hit. Boundary
patches produced by `tracked-boundary-patch` follow the exact-once edit
discipline (each `original` snippet must occur exactly once in the clean
file), so patches stay deterministic `git apply` inputs.
