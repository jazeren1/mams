# Verification Standard

## Automated checks

File exists; size is nonzero; MediaInfo parses; runtime is plausible; video/audio exist; container is MKV; SHA-256 is computed; destination checksum matches source.

## Human checks in version 1

Movies: opening, chapter jumps, action scene, subtitles, audio sync, ending.

Episodes: opening, episode identity, middle, ending, subtitles, audio sync.

## Warnings

Raise review flags for short movie runtime, out-of-range episode runtime, missing English audio, missing expected subtitles, unusually small files, near-identical candidate titles, metadata mismatch, or checksum mismatch.

Version 1 target: Standard verification—MediaInfo, checksum, and playback spot check.
