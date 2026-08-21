# Viewer (placeholder)

A zero-dependency, single-file static HTML viewer for Tracked journal runs
(DAG view + actionable report) lands here. It is deliberately **not** part of
the library-core milestone — see `docs/TRACKED_LIBRARY_PLAN.md`, Phase 6.

Design constraints (ratified):

- No npm, no build step: one self-contained `viewer.html` (hand-rolled
  SVG/canvas).
- Fed by a **distiller** (`tracked-view`, part of `tools/`) that reduces a run
  directory to a small view-model JSON.
