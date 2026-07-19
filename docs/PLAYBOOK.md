# Media Archive Playbook

## 1. Prepare

Confirm disc, edition, year, season/disc number, episode list, local space, NAS space, and destination category.

## 2. Rip

Use MakeMKV and write to local `Incoming`. Do not transcode.

For movies, choose the main feature using runtime, chapters, expected contents, and playlist information.

For TV, compare MakeMKV titles against the episode count on the packaging. Keep episode-length titles and stop for review when order is uncertain.

## 3. Select tracks

Blu-ray movies: keep source video, best English audio, English subtitles, forced English subtitles. Remove foreign and unnecessary duplicate tracks.

DVD movies: keep MPEG-2 video, English AC-3 or DTS, and English subtitles.

TV: keep source video, main English audio, English image subtitles, and useful English CC-to-text tracks.

## 4. Verify with MediaInfo

Confirm container, runtime, resolution, video codec, audio, subtitles, file size, and absence of obvious truncation.

## 5. Playback spot check

Movies: opening, chapter jumps, action-heavy scene, subtitle display, audio sync, ending.

Episodes: opening, episode identity, middle, ending, subtitles, audio sync.

## 6. Identify and rename

Movies: `Title (Year).mkv`

Episodes: `Series - SxxEyy - Episode Title.mkv`

Use FileBot or a trusted metadata source. Require confirmation for ambiguous matches.

## 7. Checksum

Compute SHA-256 and store it.

## 8. Safe replacement

1. Move the old file outside Plex roots.
2. Copy the new file to its canonical destination.
3. Verify destination checksum.
4. Trigger Plex scan.
5. Confirm Plex sees the new file.
6. Delete the temporary backup.
7. Mark complete.

Never delete the old file before destination verification.

## 9. Cleanup

Delete local staging only after checksum match, Plex verification, database update, and backup cleanup.

## 10. Failure handling

Retain source and backup, set status to `NEEDS_REVIEW`, and log the last successful step and error.
