# Verification Standard

## Automated checks

File exists; size is nonzero; MediaInfo parses; runtime is plausible; video/audio exist; container is MKV; SHA-256 is computed; destination checksum matches source.

Two distinct verification passes exist in the codebase, at two distinct
times:

- **Plan-time** (`verification.py`, Milestone 7B): judges whether the
  *source* file, before it ever moves, is structurally plausible enough
  to propose a plan for — MediaInfo probe success, non-zero size,
  plausible duration, video/audio track presence, no blocking findings.
  No checksum is computed at this stage; there is nothing to compare
  it against yet.
- **Post-transfer** (`execution.verify_destination()`, Milestone 8): a
  *fresh* MediaInfo probe against the file that actually landed at the
  destination — never the plan-time snapshot, which only describes the
  source before it moved. For a cross-filesystem copy, this is where
  "destination checksum matches source" is literally checked: SHA-256 is
  computed once while streaming the copy and once more by independently
  re-reading the fully-written file from disk, and both must match
  before the transfer is considered committed. A same-filesystem move
  (a hard link) makes byte-identity structural rather than something a
  second checksum needs to prove — see `docs/EXECUTION-SAFETY.md` for
  the full policy and the state machine this feeds into.

## Human checks in version 1

Movies: opening, chapter jumps, action scene, subtitles, audio sync, ending.

Episodes: opening, episode identity, middle, ending, subtitles, audio sync.

## Warnings

Raise review flags for short movie runtime, out-of-range episode runtime, missing English audio, missing expected subtitles, unusually small files, near-identical candidate titles, metadata mismatch, or checksum mismatch.

Version 1 target: Standard verification—MediaInfo, checksum, and playback spot check.
