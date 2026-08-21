"""``tracked-view``: distill journal runs into the viewer's view model + HTML.

See ``viewer/README.md`` for the view-model contract and the design record.
"""

from tracked_tools.view.distill import VIEW_MODEL_SCHEMA, build_html, distill_run

__all__ = ["VIEW_MODEL_SCHEMA", "build_html", "distill_run"]
