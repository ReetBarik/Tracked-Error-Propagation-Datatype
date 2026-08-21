#pragma once
// Library and journal-schema version macros.
//
// This header is committed (not configure-generated) so consumers that vendor
// the headers without CMake — e.g. a git-subtree checkout added via a raw
// include path — still get it. CMakeLists.txt parses the version out of this
// file, so this is the single source of truth; bump it here.

#define TRACKED_VERSION_MAJOR 1
#define TRACKED_VERSION_MINOR 0
#define TRACKED_VERSION_PATCH 0
#define TRACKED_VERSION_STRING "1.0.0"

// On-disk JSONL journal schema version. See docs/SCHEMA.md (normative).
//   1 = header record as line 1, optional cap/exact_tie record keys,
//       non-finite string sentinels ("nan"/"inf"/"-inf"), emitter-side JSON
//       escaping, validated scope grammar.
// (0 was the headerless pre-v1 stream: 9 keys, no header line, NaN->null,
// +/-Inf clamped to +/-DBL_MAX. Readers need an explicit legacy mode for it.)
#define TRACKED_JOURNAL_SCHEMA_VERSION 1
