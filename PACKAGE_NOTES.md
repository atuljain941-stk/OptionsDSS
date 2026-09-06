# Release package notes

This source package intentionally excludes local runtime/user data files such as:

- `options_data.db`
- `*.db`, `*.sqlite`, `*.sqlite3`
- SQLite WAL/SHM/journal files
- Python bytecode/cache folders

Do not include local database files in release ZIPs. Users should unzip code updates over their existing application folder without overwriting their local data.
