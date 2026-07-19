# Plex Integration

## Observed behavior

District 9 showed that Plex indexes every playable file, can create duplicates when both old and new files remain, and removes the old entry after the old file is deleted and the library is rescanned.

## Version 1 strategy

Use Plex for scan requests and path confirmation. Avoid direct metadata manipulation.

## Replacement sequence

Move old file outside Plex roots; copy new file; verify checksum; scan relevant library; confirm the new path; delete backup; record completion.

Future integration may use a Plex token to scan a section and query an item by file path.
