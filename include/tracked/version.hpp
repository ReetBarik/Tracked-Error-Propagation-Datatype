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

// On-disk JSONL journal schema version.
//   0 = headerless pre-v1 stream (the v0.3/v0.4 record format: 9 keys, no
//       header line, NaN->null and +/-Inf clamped to +/-DBL_MAX).
// Version 1 adds a mandatory header record as line 1 of every journal; readers
// hard-require it or explicitly enter legacy mode.
#define TRACKED_JOURNAL_SCHEMA_VERSION 0
