# Development

Use a source checkout on Windows with Python 3.11 or newer. User setup and operation are covered in README.md.

## Setup and tests

Run `Setup LiveDesk.cmd` in the checkout, then open PowerShell in that folder and run:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -v
```

Tests are in `tests/`. They use temporary databases and mocked services to cover collection, recovery, preferences, retention, exports, local settings requests, release validation, and update rollback. Do not run `collector.py` directly alongside a background collector; use the launchers for manual app checks.

## Build a release ZIP

Keep the default `config.json` streamer list empty, update `VERSION`, and review `PUBLIC_FILES` in `build_release.py` if application files were added or removed.

```powershell
.\.venv\Scripts\python.exe -B build_release.py ..\LiveDesk-windows.zip
```

The builder packages the application files and a `release.json` manifest used by the updater. Tests, developer tools, databases, logs, environments, and backups are excluded. GitHub's automatic source archives do not contain this manifest and cannot be used by the updater.

Before publishing a release, check setup, start, stop, and update in a fresh Windows installation folder. Confirm that an update preserves settings and recorded messages, and that first-run installations start with no streamers. Publish the built ZIP as a release asset.

## Working files

- `collector.py` and `control.py`: chat collection and background supervision.
- `dashboard.py`, the HTML pages, and `settings_api.py`: local dashboard and settings API.
- `preferences.py`: persisted preferences and retention.
- `bootstrap.py`, `app.py`, and `updater.py`: installation, lifecycle, and updates.
- `diagnostics.py` and `startup.py`: diagnostic reports and optional login startup.

The optional `official` collection mode uses `YOUTUBE_API_KEY` and the generated Google gRPC protocol files. The standard Windows setup uses yt-dlp and needs no API key. Preserve the attribution in NOTICE.txt when changing protocol files.

Do not commit local databases, logs, backups, `.venv`, or account credentials. The repository's `.gitignore` covers the standard generated files.
