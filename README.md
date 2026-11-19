# LiveDesk

LiveDesk is a Windows app for monitoring public YouTube livestream chats. It saves regular chat and Super Chats locally, with a dashboard for live activity and a searchable archive of past streams.

[Download for Windows](https://github.com/wilsonbc93/LiveDesk/releases/latest)

No Google API key or YouTube sign-in is needed for accessible public streams. LiveDesk downloads chat only, without video or audio.

## Install and start

You need Windows, **Python 3.11 or newer**, and an internet connection. If Python is not installed, get it from the [official Python Windows downloads page](https://www.python.org/downloads/windows/).

1. Download the LiveDesk ZIP attached to a GitHub release, or clone/download this repository.
2. Extract the ZIP into a writable folder, such as `Documents\LiveDesk`. Open the extracted folder containing the launchers; do not run them from inside the ZIP.
3. Double-click **Setup LiveDesk.cmd**. It creates a private Python environment and installs the required packages. Setup needs internet access and does not require administrator access.
4. Double-click **Start LiveDesk.cmd**. It starts collection and opens the dashboard at [http://127.0.0.1:8765](http://127.0.0.1:8765/).
5. Open **Settings**, enter a YouTube handle or channel URL, and click **Add streamer**.

New installations start with an empty streamer list. Added streamers default to recording both regular chat and Super Chats. You do not need to edit configuration files. Windows Explorer may hide the `.cmd` filename extension.

## Everyday use

| Action | How |
| --- | --- |
| Start collection and open the dashboard | Double-click **Start LiveDesk.cmd** |
| Stop collection and the dashboard | Double-click **Stop LiveDesk.cmd** |
| Pause collection while keeping the dashboard open | Use **Pause collection** in Settings |
| Resume collection | Use **Resume collection** in Settings |
| Open the dashboard while it is running | Visit [http://127.0.0.1:8765](http://127.0.0.1:8765/) |

Closing your browser does not stop collection. Your computer must stay awake and connected to record messages. Messages sent while collection is stopped, paused, or disconnected may be missed.

To start automatically after signing into Windows, enable **Start LiveDesk when I sign in to Windows** in Settings. This is off by default. It starts in the background without opening the browser and respects your saved pause setting. Turn the switch off before moving the installation folder, then enable it again from the new location if desired.

## Dashboard and history

**Live Dashboard** separates live streamers from offline/upcoming streamers. Live cards show current viewers, time live, captured USD donations, the top three donors, and a **Watch Now** link. Select a streamer to filter the live feed. The page shows the latest 200 regular messages and 60 recent paid messages; the databases retain the full captured history subject to your retention settings.

**Pause chat** on the Live Dashboard freezes only the display; recording continues. Use Settings to pause collection.

Donation totals include Super Chats and Super Stickers. Bare `$` amounts are treated as US dollars; explicitly marked other currencies are excluded. Totals cover captured purchase amounts, not creator earnings or payments that were missed.

**Historical Data** lets you find past streams by streamer, title, and date. Choose a stream to view donation totals, top donors, and paginated chat and Super Chat histories. Message search checks message text; username search checks the sender. You can use both filters together. Dates use your local timezone, and a stream's date falls back to its first saved message if its start time is unavailable.

Historical Data contains records captured by LiveDesk. It does not automatically download chat from streams that ended before monitoring began.

## User Search

Use **User Search** to find a viewer across all captured live and historical streams. Search their username or full YouTube channel ID, then select an account to see its saved names, first/last activity, chat and Super Chat counts, streamers visited, and captured USD donations.

Filter the activity feed by streamer, stream, date, message text, or message type. Click a streamer summary to filter its messages. Clicking a sender in the Live Dashboard or Historical Data opens that account directly.

Accounts are grouped by YouTube channel ID, so name changes do not split an identified account and identical names do not combine different accounts. Records without an ID are kept as separate **unverified name-only matches**; records sharing a name cannot be confirmed as one person. This page covers captured activity only, including retained records from removed streamers.

## Settings

| Setting | What it does |
| --- | --- |
| **Monitor** | The master switch for a streamer. Turning it off stops checks and recording, while preserving their recording choices and saved history. |
| **Live chat / Super Chats** | Choose either message type or both for each streamer. |
| **Remove** | Stops monitoring a streamer and removes them from the monitored list. Existing saved records remain available. |
| **Check for live streams** | How often to check live status: 15–3,600 seconds, default 60. Chat arrives at YouTube's own update cadence. |
| **Dashboard refresh** | How often the Live Dashboard refreshes: 1–60 seconds, default 2. |
| **Keep normal chat / Keep Super Chats** | Separate retention limits in days. **0 keeps everything.** |
| **Export records** | Download CSV filtered by message type, streamer, stream, and date range. |

Retention limits permanently delete expired records based on when LiveDesk received them, including records from removed streamers. The page asks for confirmation before applying a limit. Cleanup runs after a policy change and hourly while the collector is running, including when collection is paused. Deleted records free space for future messages, but database file sizes may not shrink.

Changes apply automatically. Changing the monitored list or live-check interval can briefly reconnect active collectors. Recording only Super Chats still requires receiving the chat feed to find paid messages, but ordinary messages are not stored.

## Update

1. Download the **LiveDesk release ZIP** attached to the new GitHub release. The updater requires this packaged release; GitHub's automatic **Source code** ZIP is not an update package.
2. Run **Update LiveDesk.cmd** from your existing installation.
3. Select the downloaded ZIP, check the displayed version, and confirm.

The updater checks the package, prepares dependencies, backs up the installation and databases, and preserves your settings and history. It briefly stops the app and restarts whichever components were running. If file installation or verification fails, it restores the previous application files and Python environment.

Backups remain in `backups/<timestamp>/`. They include database snapshots and the previous environment, so they can use substantial disk space. Keep the backups you need and remove older ones after checking the update. Database snapshots are not restored automatically on rollback.

## Local data and backups

LiveDesk stores its files in the installation folder:

| File | Contents |
| --- | --- |
| `settings.sqlite3` | Monitored streamers, recording choices, timing, retention, and pause state |
| `chat-messages.sqlite3` | Regular chat messages and author information |
| `superchats.sqlite3` | Super Chats, stickers, donor information, and stream metadata |
| `config.json` | Database locations and initial defaults; manage streamers through Settings |

To make a backup, run **Stop LiveDesk.cmd**, then copy all three databases and `config.json` to a safe location. Keep these files when updating or moving the app. To restore a backup, stop LiveDesk, replace these files with the saved copies, then start it again. Copying only a database's main file while collection is running can omit recent writes.

The dashboard is accessible only on this computer. Chat data is stored locally and is not uploaded by LiveDesk. CSV exports protect formula-like author and message text with a leading apostrophe when opened in a spreadsheet.

## Troubleshooting

Open **Settings → Help & diagnostics** for collection status, recent errors, and a diagnostic download. The diagnostic ZIP contains health information and redacted log tails. It excludes chat and donation contents, databases, and full configuration. Nothing is uploaded automatically; review the report before sharing it.

| Problem | What to check |
| --- | --- |
| **Python not found** | Install Python 3.11 or newer, then run Setup again. |
| **Setup fails** | Check your internet connection, confirm the ZIP is extracted into a writable folder, and read `setup.log`. Rerunning Setup installs or repairs dependencies without clearing your data. |
| **No messages appear** | Confirm the streamer is live, collection is running, global pause is off, Monitor is enabled, and the recording types you want are checked. |
| **Port 8765 is busy** | Stop another LiveDesk copy or application using that port, then start LiveDesk again. |
| **YouTube checks or chat connections fail** | Check your connection and the collection health information. Temporary failures retry automatically. Restricted chats may be unavailable. |
| **Login startup points to an old folder** | If the old folder no longer exists, press Win+R, enter `shell:startup`, and remove its old LiveDesk shortcut. Enable startup from the new installation if needed. |

YouTube can change its chat interfaces, so a LiveDesk update may be needed if connections stop working. Capture cannot reconstruct removed messages or guarantee a complete donation ledger. When a channel has simultaneous streams, discovery may find only one of them.

Third-party attribution is in [NOTICE.txt](NOTICE.txt).
