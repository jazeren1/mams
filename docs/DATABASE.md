# Database Design

SQLite is the version 1 database.

Main entities:

- `discs`: physical media inventory
- `assets`: movies, episodes, and bonus features
- `files`: staging, destination, backup, and old-version files
- `jobs`: workflow execution
- `replacements`: upgrade and replacement history
- `events`: append-only audit log

Schema changes must be versioned rather than applied manually to a deployed database.
