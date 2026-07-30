# Palworld Server Manager

Windows manager for a dedicated Palworld server: SteamCMD install/update, hourly backups, crash recovery, daily clock-based restarts, and REST API control.

Based on [deafdudecomputers/PalworldServer](https://github.com/deafdudecomputers/PalworldServer) by Pylar1991, customized for config-driven paths, visible `PalServer.exe` launches, and REST-only admin (no RCON / PalDefender / public lobby).

## Requirements

- Windows
- [Python 3](https://www.python.org/downloads/) (Tkinter included with the standard Windows installer)
- Network access for SteamCMD and server updates

## Quick start

1. Clone this repository.
2. Copy `config.example.json` to `config.json` (or run once and the manager will copy it for you).
3. Edit `config.json` if you need different paths or restart time (default daily restart: `05:00` local time).
4. Double-click `start_server.cmd`.
5. Click **Start Server** and let SteamCMD download the dedicated server on first run.
6. After the first boot, stop the server and configure `server/Pal/Saved/Config/WindowsServer/PalWorldSettings.ini`.

### Required `PalWorldSettings.ini` settings

```ini
AdminPassword="YourStrongPassword"
RESTAPIEnabled=True
RESTAPIPort=8212
PublicPort=8211
```

The manager reads `AdminPassword`, `RESTAPIPort`, and `PublicPort` from this file. Stats and graceful stop/restart will not work if `AdminPassword` is empty or REST API is disabled.

Official REST API docs: [docs.palworldgame.com/category/rest-api](https://docs.palworldgame.com/category/rest-api/)

## Configuration (`config.json`)

| Key | Purpose |
|-----|---------|
| `paths.*` | Relative or absolute paths for SteamCMD, install, settings, backups, logs |
| `paths.server_exe` | Launcher binary (default `server/PalServer.exe`) |
| `paths.shipping_exe` | Long-lived process used for status/kill (`.../PalServer-Win64-Shipping-Cmd.exe`) |
| `restart_time` | Daily restart clock time (`HH:MM`, local), e.g. `"05:00"` |
| `server_address` | REST API host (usually `127.0.0.1`) |
| `query_port` | Starting Steam query port (auto-increments if busy) |
| `max_save_age_minutes` | Stale `Level.sav` threshold before forced restart checks |
| `shutdown_wait_seconds` | Graceful REST shutdown wait time |
| `messages.*` | In-game announce / shutdown texts (edit here, not in Python) |

### Messages (`messages`)

| Key | When it is used |
|-----|-----------------|
| `restart_1h` / `restart_30m` / `restart_10m` | Daily restart countdown warnings |
| `restart_minutes` | 5–1 minute warnings; use `{minutes}` placeholder |
| `restart_now` | Final warning under 60 seconds |
| `daily_shutdown` | REST shutdown message for the daily restart |
| `update_announce` / `update_shutdown` | Steam update restart |
| `stale_save_announce` / `stale_save_shutdown` | Stale save restart |
| `manual_stop` | Stop button / window close |
| `default_shutdown` | Fallback shutdown text |

`config.json` is gitignored so local path overrides stay private. Commit `config.example.json` only.

## Features

- Auto-download SteamCMD and install/update Palworld dedicated server (app `2394010`)
- Launch via visible `PalServer.exe` (console not hidden)
- Daily restart at a fixed time from config, with in-game announce warnings
- Graceful stop/restart: REST `save` → `shutdown`, then `stop`, then process kill as fallback
- Player count from REST `/v1/api/players` (falls back to metrics)
- Hourly zip backups of `Pal/Saved`
- Crash and stale-save auto-restart
- Optional `Engine.ini` performance overrides copied into the server config on update

## UI

Shows **Status**, **Version**, **Players**, and **Next restart**. Start/Stop controls the dedicated process.

## What is not in git

See [`.gitignore`](.gitignore). Notably:

- `Source/` — local upstream reference install (keep on your machine; not published)
- `server/`, `steamcmd/`, `backups/`, `logs/`, `server_updates/`
- `config.json`

## Port forwarding

Forward your `PublicPort` (game) and usually the query port. Keep `RESTAPIPort` on localhost / LAN only; do not expose the REST API to the public internet.

## License / credit

Upstream project: [deafdudecomputers/PalworldServer](https://github.com/deafdudecomputers/PalworldServer). This repository is an independent customization of that tool.
