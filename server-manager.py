import os
import time
import shutil
import zipfile
import base64
import json
import subprocess
import threading
import urllib.request
import re
import socket
import tkinter as tk
from tkinter import ttk, scrolledtext, StringVar, messagebox
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
CONFIG_EXAMPLE_PATH = os.path.join(SCRIPT_DIR, "config.example.json")
STEAMCMD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
GAME_APP_ID = "2394010"
API_USER = "admin"
SERVER_PROCESS_NAMES = (
    "PalServer.exe",
    "PalServer-Win64-Shipping-Cmd.exe",
    "PalServer-Win64-Shipping.exe",
)

def resolve_path(value):
    if not value:
        return value
    if os.path.isabs(value):
        return value
    return os.path.normpath(os.path.join(SCRIPT_DIR, value))

def load_config():
    if not os.path.isfile(CONFIG_PATH):
        if not os.path.isfile(CONFIG_EXAMPLE_PATH):
            raise FileNotFoundError("Missing config.json and config.example.json")
        shutil.copy2(CONFIG_EXAMPLE_PATH, CONFIG_PATH)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

CONFIG = load_config()
PATHS = CONFIG.get("paths", {})

STEAMCMD_DIR = resolve_path(PATHS.get("steamcmd_dir", "steamcmd"))
STEAMCMD_EXE = os.path.join(STEAMCMD_DIR, "steamcmd.exe")
STEAMCMD_ZIP = os.path.join(SCRIPT_DIR, "steamcmd.zip")
INSTALL_DIR = resolve_path(PATHS.get("install_dir", "server"))
TARGET_EXE = resolve_path(PATHS.get("server_exe", "server/PalServer.exe"))
SERVER_ID_FILE = resolve_path(PATHS.get(
    "server_id_file",
    "server/Pal/Saved/Config/WindowsServer/GameUserSettings.ini",
))
SERVER_SETTINGS_FILE = resolve_path(PATHS.get(
    "settings_file",
    "server/Pal/Saved/Config/WindowsServer/PalWorldSettings.ini",
))
BACKUP_PATH = resolve_path(PATHS.get("backup_source", "server/Pal/Saved"))
BACKUP_DIR = resolve_path(PATHS.get("backup_dir", "backups"))
LOG_DIR = resolve_path(PATHS.get("log_dir", "logs"))
ENGINE_INI = resolve_path(PATHS.get("engine_ini", "Engine.ini"))

RESTART_TIME = CONFIG.get("restart_time", "05:00")
MAX_SAVE_AGE_MINUTES = int(CONFIG.get("max_save_age_minutes", 10))
SHUTDOWN_WAIT_SECONDS = int(CONFIG.get("shutdown_wait_seconds", 60))
server_address = CONFIG.get("server_address", "127.0.0.1")
server_query_port = str(CONFIG.get("query_port", "27015"))

DEFAULT_MESSAGES = {
    "restart_1h": "Server will restart in 1 hour!",
    "restart_30m": "Server will restart in 30 minutes!",
    "restart_10m": "Server will restart in 10 minutes! Please save your progress.",
    "restart_minutes": "Server will restart in {minutes} minute(s)! Please save your progress.",
    "restart_now": "Server restarting now!",
    "daily_shutdown": "Daily scheduled restart.",
    "update_announce": "Server restarting for update in 30 seconds!",
    "update_shutdown": "Updating server, please reconnect shortly.",
    "stale_save_announce": "Server restarting due to stale save file in 30 seconds!",
    "stale_save_shutdown": "Restarting due to stale save.",
    "manual_stop": "Server stopped by administrator.",
    "default_shutdown": "Server shutting down.",
    "no_install": "Start the server to install Palworld server using SteamCMD",
}
MESSAGES = {**DEFAULT_MESSAGES, **(CONFIG.get("messages") or {})}

start_thread = None
is_starting = False
manual_stop = False
start_time = time.time()
update_pending = False
last_checked_minute = None
save_stale_stacks = 0
server_restapi_port = None
admin_password = None
SERVER_PORT = None
next_scheduled_restart = None

def msg(key, **kwargs):
    text = MESSAGES.get(key, DEFAULT_MESSAGES.get(key, key))
    try:
        return text.format(**kwargs) if kwargs else text
    except (KeyError, ValueError):
        return text

# --- Settings / config helpers -------------------------------------------------

def get_dedicated_name():
    if not os.path.isfile(SERVER_ID_FILE):
        return ""
    with open(SERVER_ID_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith("DedicatedServerName"):
                return line.split("=", 1)[1].strip()
    return ""

def get_setting_value(file_path, keyword):
    if not os.path.isfile(file_path):
        return None
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    option_line, in_section = None, False
    for line in lines:
        line = line.strip()
        if line == "[/Script/Pal.PalGameWorldSettings]":
            in_section = True
        elif in_section and line.startswith("OptionSettings="):
            option_line = line[len("OptionSettings="):].strip()
            break
    if not option_line:
        return None
    if option_line.startswith("(") and option_line.endswith(")"):
        option_line = option_line[1:-1]
    parts = re.findall(r'(?:[^,"\'\s]+|"[^"]*"|\'[^\']*\')+', option_line)
    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip() == keyword:
            return value.strip().strip('"')
    return None

def refresh_settings_from_ini():
    global server_restapi_port, admin_password, SERVER_PORT
    server_restapi_port = get_setting_value(SERVER_SETTINGS_FILE, "RESTAPIPort")
    admin_password = get_setting_value(SERVER_SETTINGS_FILE, "AdminPassword")
    SERVER_PORT = get_setting_value(SERVER_SETTINGS_FILE, "PublicPort")

def setting_is_true(value):
    return bool(value) and value.strip().strip('"').lower() in ("true", "1", "yes")

def is_palworld_settings_empty(path=None):
    path = path or SERVER_SETTINGS_FILE
    if not os.path.isfile(path):
        return True
    try:
        with open(path, "r", encoding="utf-8") as f:
            return "OptionSettings=" not in f.read()
    except OSError:
        return True

def ensure_palworld_settings_seeded():
    """Fill empty PalWorldSettings.ini from DefaultPalWorldSettings.ini when needed."""
    default_settings = os.path.join(INSTALL_DIR, "DefaultPalWorldSettings.ini")
    os.makedirs(os.path.dirname(SERVER_SETTINGS_FILE), exist_ok=True)
    if not is_palworld_settings_empty(SERVER_SETTINGS_FILE):
        return False
    if not os.path.isfile(default_settings):
        log(f"WARN: PalWorldSettings.ini is empty and template missing: {default_settings}")
        return False

    shutil.copy2(default_settings, SERVER_SETTINGS_FILE)
    try:
        with open(SERVER_SETTINGS_FILE, "r", encoding="utf-8") as f:
            text = f.read()
        patched = text.replace("RESTAPIEnabled=False", "RESTAPIEnabled=True")
        if patched != text:
            with open(SERVER_SETTINGS_FILE, "w", encoding="utf-8", newline="\n") as f:
                f.write(patched)
            log("Seeded PalWorldSettings.ini from default (RESTAPIEnabled=True).")
        else:
            log("Seeded PalWorldSettings.ini from DefaultPalWorldSettings.ini.")
    except OSError as e:
        log(f"Seeded PalWorldSettings.ini, but could not patch RESTAPIEnabled: {e}")

    log("Other empty WindowsServer .ini files are normal Unreal stubs.")
    log("Set AdminPassword in PalWorldSettings.ini for REST stats/announces.")
    refresh_settings_from_ini()
    return True

def check_palworld_settings():
    ensure_palworld_settings_seeded()
    refresh_settings_from_ini()
    issues = []

    if not os.path.isfile(SERVER_SETTINGS_FILE):
        issues.append(f"Missing settings file: {SERVER_SETTINGS_FILE}")
    elif is_palworld_settings_empty(SERVER_SETTINGS_FILE):
        issues.append("PalWorldSettings.ini is empty (no OptionSettings).")
    else:
        rest_enabled = get_setting_value(SERVER_SETTINGS_FILE, "RESTAPIEnabled")
        checks = [
            (bool(admin_password),
             "AdminPassword is empty — REST auth/announces/shutdown will fail."),
            (setting_is_true(rest_enabled),
             "RESTAPIEnabled is not True — set RESTAPIEnabled=True."),
            (bool(server_restapi_port),
             "RESTAPIPort is missing."),
            (bool(SERVER_PORT),
             "PublicPort is missing — game port will fall back to 8211."),
        ]
        for ok, warn in checks:
            if not ok:
                issues.append(warn)

    if not issues:
        log("Settings check: OK")
        return issues

    log("---------- PalWorldSettings check ----------")
    if os.path.isfile(SERVER_SETTINGS_FILE):
        log(f"Settings file: {SERVER_SETTINGS_FILE}")
    for warn in issues:
        log(f"WARN: {warn}")
    log(f"Settings check: {len(issues)} issue(s) — some features may be limited.")
    log("--------------------------------------------")
    return issues

# --- Schedule helpers ----------------------------------------------------------

def parse_restart_time(value):
    hour, minute = value.strip().split(":")
    return int(hour), int(minute)

def compute_next_scheduled_restart(from_dt=None):
    now = from_dt or datetime.now()
    hour, minute = parse_restart_time(RESTART_TIME)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate

def seconds_until_scheduled_restart():
    if next_scheduled_restart is None:
        return None
    return (next_scheduled_restart - datetime.now()).total_seconds()

def get_free_query_port(start_port):
    port = int(start_port)
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return str(port)
            except OSError:
                port += 1

# --- REST API ------------------------------------------------------------------

def base64_auth_info():
    return base64.b64encode(f"{API_USER}:{admin_password or ''}".encode("ascii")).decode("ascii")

def api_url(path):
    return f"http://{server_address}:{server_restapi_port}{path}"

def api_get(path, timeout=5):
    if not server_restapi_port or not admin_password:
        return None
    try:
        req = urllib.request.Request(api_url(path))
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Basic {base64_auth_info()}")
        req.add_header("User-Agent", "PalworldServerManager")
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                return None
            body = response.read().decode()
            return json.loads(body) if body else {}
    except Exception:
        return None

def api_post(path, body=None, timeout=10):
    """POST to REST API. Returns (ok, detail)."""
    if not server_restapi_port:
        return False, "RESTAPIPort not set"
    if not admin_password:
        return False, "AdminPassword empty"
    try:
        data = b"{}" if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(api_url(path), data=data, method="POST")
        req.add_header("Authorization", f"Basic {base64_auth_info()}")
        req.add_header("User-Agent", "PalworldServerManager")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode(errors="replace")
            if response.status == 200:
                return True, raw or "OK"
            return False, f"HTTP {response.status}: {raw[:300]}"
    except HTTPError as e:
        try:
            raw = e.read().decode(errors="replace")
        except Exception:
            raw = ""
        return False, f"HTTP {e.code}: {raw[:300]}"
    except (URLError, TimeoutError, OSError) as e:
        return False, str(e)

def get_server_version_from_api():
    data = api_get("/v1/api/info")
    if data and "version" in data:
        return ".".join(data["version"].split(".")[:3])
    return "Offline"

def get_online_players():
    """Return (count_str, name_list) from REST /v1/api/players."""
    data = api_get("/v1/api/players")
    if data is None:
        metrics = api_get("/v1/api/metrics")
        count = str(metrics.get("currentplayernum", 0)) if metrics else "0"
        return count, []
    players = data.get("players", data if isinstance(data, list) else [])
    if not isinstance(players, list):
        return "0", []
    names = []
    for player in players:
        if isinstance(player, dict):
            name = player.get("name") or player.get("playerName") or player.get("Name")
            if name:
                names.append(str(name))
            else:
                names.append(str(player.get("userId") or player.get("playerId") or "Unknown"))
        else:
            names.append(str(player))
    names.sort(key=str.lower)
    return str(len(names)), names

def send_server_announcement(message):
    ok, detail = api_post("/v1/api/announce", {"message": message})
    if ok:
        log(f"Announcement sent: {message}")
    else:
        log(f"Failed to send announcement: {message} ({detail})")

def save_world():
    ok, detail = api_post("/v1/api/save", timeout=60)
    if ok:
        log("World save requested via REST API.")
    else:
        log(f"World save via REST API failed: {detail}")
        log("Note: game-side 'Failed copy from backup' is a Palworld permissions/disk issue.")
    return ok

def request_shutdown(waittime, message):
    ok, detail = api_post(
        "/v1/api/shutdown",
        {"waittime": int(waittime), "message": message},
        timeout=30,
    )
    if ok:
        log(f"Graceful shutdown requested ({waittime}s): {message}")
    else:
        log(f"Graceful shutdown via REST API failed: {detail}")
    return ok

def request_force_stop():
    ok, detail = api_post("/v1/api/stop", timeout=15)
    if ok:
        log("Force stop requested via REST API.")
    else:
        log(f"Force stop via REST API failed: {detail}")
    return ok

# --- UI helpers ----------------------------------------------------------------

def apply_dark_theme(window):
    style = ttk.Style(window)
    style.theme_use("clam")
    style.configure("Dark.TButton", background="#555555", foreground="white", font=("Arial", 10), padding=6)
    style.map("Dark.TButton", background=[("active", "#666666"), ("!disabled", "#555555")])
    style.configure("TFrame", background="#2f2f2f")
    style.configure("TLabel", background="#2f2f2f", foreground="white")
    window.configure(bg="#2f2f2f")

def update_player_list(names):
    def apply():
        try:
            current = list(player_listbox.get(0, tk.END))
            if current == names:
                return
            player_listbox.delete(0, tk.END)
            for name in names:
                player_listbox.insert(tk.END, name)
        except tk.TclError:
            pass

    try:
        if threading.current_thread() is threading.main_thread():
            apply()
        else:
            root.after(0, apply)
    except Exception:
        pass

def log(message_text):
    now = datetime.now().strftime("[%I:%M:%S%p]").lstrip("0")
    line = f"{now} {message_text}"
    os.makedirs(LOG_DIR, exist_ok=True)
    try:
        with open(os.path.join(LOG_DIR, f"server_{datetime.now():%Y-%m-%d}.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass

    def append_ui():
        try:
            log_box.config(state=tk.NORMAL)
            log_box.insert(tk.END, line + "\n")
            log_box.see(tk.END)
            log_box.config(state=tk.DISABLED)
        except tk.TclError:
            pass

    try:
        if threading.current_thread() is threading.main_thread():
            append_ui()
        else:
            root.after(0, append_ui)
    except Exception:
        pass

# --- SteamCMD / install --------------------------------------------------------

def download_and_extract_steamcmd():
    if os.path.exists(STEAMCMD_EXE):
        return
    log("SteamCMD not found. Downloading...")
    try:
        os.makedirs(STEAMCMD_DIR, exist_ok=True)
        urllib.request.urlretrieve(STEAMCMD_URL, STEAMCMD_ZIP)
        with zipfile.ZipFile(STEAMCMD_ZIP, "r") as zip_ref:
            zip_ref.extractall(STEAMCMD_DIR)
        os.remove(STEAMCMD_ZIP)
        log("SteamCMD installed.")
    except Exception as e:
        log(f"Failed to download SteamCMD: {e}")
    steamcmd_initialize()

def steamcmd_initialize():
    init_log = os.path.join(STEAMCMD_DIR, "init.log")
    log("SteamCMD initialization started...")
    with open(init_log, "w", encoding="utf-8") as out:
        subprocess.run([STEAMCMD_EXE, "+login", "anonymous", "+quit"], text=True, stdout=out, stderr=out)
    log("SteamCMD initialization finished.")

def extract_buildid_from_file(file_path):
    with open(file_path, "r", encoding="utf-8") as file:
        for line in file:
            if '"buildid"' in line:
                return line.split('"')[3]
    return None

def check_update():
    global last_checked_minute, update_pending
    current_minute = time.localtime().tm_min
    if last_checked_minute == current_minute:
        return
    log("Now checking for update...")
    last_checked_minute = current_minute

    appinfo_dir = os.path.join(INSTALL_DIR, "steamapps")
    os.makedirs(appinfo_dir, exist_ok=True)
    buildid_output_file = os.path.join(INSTALL_DIR, "buildid_output.log")
    appinfo_file = os.path.join(appinfo_dir, f"appmanifest_{GAME_APP_ID}.acf")
    steamcmd_cmd = [
        STEAMCMD_EXE, "+login", "anonymous", "+app_info_update", str(GAME_APP_ID),
        "+app_info_print", str(GAME_APP_ID), "+logoff", "+quit",
    ]
    with open(buildid_output_file, "w", encoding="utf-8") as out:
        try:
            subprocess.run(steamcmd_cmd, text=True, stdout=out, stderr=out, check=True)
        except subprocess.CalledProcessError:
            return

    buildid = extract_buildid_from_file(buildid_output_file)
    old_buildid = extract_buildid_from_file(appinfo_file) if os.path.exists(appinfo_file) else None
    log(f"[BuildID] Current: {old_buildid} | New: {buildid}")

    if old_buildid != buildid:
        first_install = old_buildid is None
        if first_install:
            log("First install detected. Downloading dedicated server files...")
            log("SteamCMD download in progress (this often takes several minutes). Please wait...")
        else:
            log("Server update detected, starting update...")
            if is_server_running():
                send_server_announcement(msg("update_announce"))
                update_pending = True
                graceful_stop(waittime=30, message=msg("update_shutdown"))
            else:
                log("Server is not running; skipping announce/shutdown before update.")
            log("SteamCMD update download in progress (this often takes several minutes). Please wait...")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        update_dir = os.path.join(SCRIPT_DIR, "server_updates", f"Build_{buildid}_{timestamp}")
        os.makedirs(update_dir, exist_ok=True)
        os.makedirs(LOG_DIR, exist_ok=True)
        cmd = [
            STEAMCMD_EXE, "+login", "anonymous",
            "+force_install_dir", update_dir,
            "+app_update", GAME_APP_ID, "validate", "+quit",
        ]
        with open(os.path.join(LOG_DIR, "steamcmd_update.log"), "w", encoding="utf-8") as out:
            subprocess.run(cmd, stdout=out, stderr=out)
        log(f"Download finished. Files staged at {update_dir}")
        log("Copying files into the server install directory...")
        for item in os.listdir(update_dir):
            src = os.path.join(update_dir, item)
            dst = os.path.join(INSTALL_DIR, item)
            if os.path.isdir(src):
                shutil.copytree(
                    src, dst, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("Engine.ini", "PalWorldSettings.ini"),
                )
            else:
                shutil.copy2(src, dst)
        if os.path.isfile(ENGINE_INI):
            dest_engine = os.path.join(INSTALL_DIR, "Pal", "Saved", "Config", "WindowsServer", "Engine.ini")
            os.makedirs(os.path.dirname(dest_engine), exist_ok=True)
            shutil.copy2(ENGINE_INI, dest_engine)
        log("First install complete." if first_install else "Update files copied to main install directory.")
        if not os.path.isfile(TARGET_EXE):
            log(f"WARNING: Expected server executable missing: {TARGET_EXE}")
    else:
        log("No update detected.")

    ensure_palworld_settings_seeded()
    if os.path.exists(buildid_output_file):
        os.remove(buildid_output_file)

def perform_backup():
    datestamp = datetime.now().strftime("%Y-%m-%d")
    hourstamp = datetime.now().strftime("%H")
    daily_folder = os.path.join(BACKUP_DIR, f"Backup_{datestamp}")
    os.makedirs(daily_folder, exist_ok=True)
    backup_file = os.path.join(daily_folder, f"Backup_{hourstamp}.zip")
    if os.path.exists(backup_file):
        return
    log("Starting backup...")
    try:
        with zipfile.ZipFile(backup_file, "w", zipfile.ZIP_DEFLATED) as backup_zip:
            for foldername, _subfolders, filenames in os.walk(BACKUP_PATH):
                for filename in filenames:
                    filepath = os.path.join(foldername, filename)
                    arcname = os.path.join(
                        os.path.basename(BACKUP_PATH),
                        os.path.relpath(filepath, BACKUP_PATH),
                    )
                    backup_zip.write(filepath, arcname)
        log(f"Backup completed: Backup_{datestamp}/Backup_{hourstamp}.zip")
    except Exception as e:
        log(f"Backup failed: {e}")

# --- Process management --------------------------------------------------------

def list_server_pids():
    """PIDs for PalServer / Shipping processes under this install."""
    install_norm = os.path.normcase(os.path.abspath(INSTALL_DIR))
    names_ps = ",".join(f"'{n}'" for n in SERVER_PROCESS_NAMES)
    ps_script = f"""
    $names = @({names_ps})
    Get-CimInstance Win32_Process | Where-Object {{ $names -contains $_.Name }} | ForEach-Object {{
      $exe = if ($_.ExecutablePath) {{ $_.ExecutablePath }} else {{ '' }}
      $cmd = if ($_.CommandLine) {{ $_.CommandLine }} else {{ '' }}
      Write-Output ("{{0}}`t{{1}}`t{{2}}" -f $_.ProcessId, $exe, $cmd)
    }}
    """
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True,
        )
        pids = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or "\t" not in line:
                continue
            parts = line.split("\t", 2)
            pid = parts[0].strip()
            exe = os.path.normcase(parts[1].strip()) if len(parts) > 1 else ""
            cmd = os.path.normcase(parts[2].strip()) if len(parts) > 2 else ""
            if pid.isdigit() and (install_norm in exe or install_norm in cmd):
                pids.append(pid)
        return list(dict.fromkeys(pids))
    except Exception as e:
        log(f"WARN: process scan failed: {e}")
        return []

def is_server_running():
    return bool(list_server_pids())

def is_server_installed():
    return os.path.isfile(TARGET_EXE)

def pid_exists(pid):
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True,
        )
        out = (result.stdout or "").strip().lower()
        return str(pid) in out and "no tasks" not in out
    except Exception:
        return False

def wait_for_process_exit(timeout_seconds):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not is_server_running():
            return True
        time.sleep(0.5)
    return not is_server_running()

def kill_server_processes(preferred_pids=None):
    pids = list(dict.fromkeys([*(preferred_pids or []), *list_server_pids()]))
    alive = [pid for pid in pids if pid_exists(pid)]
    gone = [pid for pid in pids if pid not in alive]
    if gone:
        log(f"Already exited (skipping kill): {', '.join(gone)}")
    if alive:
        log(f"Force-killing server PID(s): {', '.join(alive)}")
        for pid in alive:
            completed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, text=True,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                if detail and "not found" not in detail.lower():
                    log(f"taskkill PID {pid}: {detail}")
    elif not pids:
        log("No matching PIDs from scan; trying taskkill by image name...")
        for image in SERVER_PROCESS_NAMES:
            subprocess.run(
                ["taskkill", "/F", "/T", "/IM", image],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    time.sleep(1)
    left = list_server_pids()
    if left:
        log(f"WARN: still running after kill: {', '.join(left)}")
    else:
        log("All server processes terminated.")

def graceful_stop(waittime=None, message=None):
    waittime = SHUTDOWN_WAIT_SECONDS if waittime is None else int(waittime)
    if message is None:
        message = msg("default_shutdown")
    refresh_settings_from_ini()

    pids_before = list_server_pids()
    if not pids_before:
        log("Server is not running; nothing to stop.")
        return True

    log(f"Server PIDs before stop: {', '.join(pids_before)}")
    if get_server_version_from_api() != "Offline":
        save_world()
        time.sleep(2)
        if request_shutdown(waittime, message):
            wait_for_process_exit(waittime + 5)
        else:
            request_force_stop()
            wait_for_process_exit(8)

    log("Ensuring server processes are killed...")
    kill_server_processes(preferred_pids=pids_before)
    wait_for_process_exit(10)
    if list_server_pids():
        log("WARN: Server process still detected after stop.")
        return False
    log("Server stop complete.")
    return True

# --- Start / stop / monitor ----------------------------------------------------

def start_server():
    global start_thread, is_starting
    refresh_settings_from_ini()
    if start_thread and start_thread.is_alive():
        log("Start server is already in progress...")
        return
    is_starting = True
    btn_start.config(state=tk.DISABLED)
    btn_stop.config(state=tk.NORMAL)
    start_thread = threading.Thread(target=_start_server, daemon=True)
    start_thread.start()

def _start_server():
    global manual_stop, server_query_port
    manual_stop = False
    refresh_settings_from_ini()
    check_palworld_settings()
    if is_server_running():
        kill_server_processes()
    download_and_extract_steamcmd()
    check_update()
    port = SERVER_PORT or "8211"
    if not SERVER_PORT:
        log("PublicPort missing from PalWorldSettings.ini; using 8211.")
    server_query_port = get_free_query_port(server_query_port)
    log(f"Using QueryPort {server_query_port}")
    log("Starting server, please wait...")
    if not os.path.isfile(TARGET_EXE):
        log(f"Server executable not found: {TARGET_EXE}")
        root.after(0, lambda: (btn_start.config(state=tk.NORMAL), btn_stop.config(state=tk.DISABLED)))
        return
    subprocess.Popen([
        TARGET_EXE,
        "-useperfthreads",
        "-UseMultithreadForDS",
        f"-port={port}",
        f"-QueryPort={server_query_port}",
    ], cwd=os.path.dirname(TARGET_EXE))

def stop_server():
    global manual_stop, is_starting
    manual_stop = True
    is_starting = False
    btn_stop.config(state=tk.DISABLED)
    btn_start.config(state=tk.DISABLED)
    log("Stopping server...")
    threading.Thread(
        target=lambda: graceful_stop(waittime=10, message=msg("manual_stop")),
        daemon=True,
    ).start()

def log_restart(reason):
    now = datetime.now().strftime("%I:%M:%S%p").lstrip("0")
    log(f"========== RESTART: {reason} ==========")
    log(f"Time: {now} | Players: {players_var.get()} | Next daily restart: {RESTART_TIME}")

def maybe_send_restart_warnings(time_to_restart, warning_keys_sent):
    thresholds = [
        (3600, "1h", "restart_1h"),
        (1800, "30m", "restart_30m"),
        (600, "10m", "restart_10m"),
    ]
    for seconds, key, message_key in thresholds:
        if 0 < time_to_restart <= seconds and key not in warning_keys_sent:
            send_server_announcement(msg(message_key))
            warning_keys_sent.add(key)
    for minutes in range(5, 0, -1):
        key = f"{minutes}m"
        if 0 < time_to_restart <= minutes * 60 and key not in warning_keys_sent:
            send_server_announcement(msg("restart_minutes", minutes=minutes))
            warning_keys_sent.add(key)
    if 0 < time_to_restart <= 60 and "final" not in warning_keys_sent:
        send_server_announcement(msg("restart_now"))
        warning_keys_sent.add("final")

def run_hourly_maintenance(save_file):
    global save_stale_stacks, start_time
    check_update()
    perform_backup()
    if not os.path.isfile(save_file):
        return
    age_minutes = (time.time() - os.path.getmtime(save_file)) / 60
    if age_minutes < MAX_SAVE_AGE_MINUTES:
        save_stale_stacks = 0
        return
    save_stale_stacks += 1
    log(f"Save file stale for {age_minutes:.1f} mins, stack {save_stale_stacks}/5")
    if save_stale_stacks < 5:
        return
    send_server_announcement(msg("stale_save_announce"))
    log_restart("stale_save")
    graceful_stop(waittime=30, message=msg("stale_save_shutdown"))
    time.sleep(2)
    _start_server()
    start_time = time.time()
    save_stale_stacks = 0

def monitor_server():
    global start_time, save_stale_stacks, manual_stop, update_pending, next_scheduled_restart, is_starting
    was_running = False
    last_checked_time = 0
    booted_message_printed = False
    boot_start_time = None
    warning_keys_sent = set()
    next_scheduled_restart = compute_next_scheduled_restart()

    while True:
        running = is_server_running()
        root.after(0, set_controls_state, running)
        dedicated_name = get_dedicated_name()
        save_file = os.path.join(INSTALL_DIR, "Pal", "Saved", "SaveGames", "0", dedicated_name, "Level.sav")

        if running:
            if not was_running:
                start_time = time.time()
                boot_start_time = time.time()
                warning_keys_sent.clear()
                is_starting = False
            was_running = True

            ver = get_server_version_from_api()
            time_to_restart = seconds_until_scheduled_restart()
            if time_to_restart is not None:
                maybe_send_restart_warnings(time_to_restart, warning_keys_sent)
            if time_to_restart is not None and time_to_restart <= 0:
                log_restart("scheduled")
                graceful_stop(waittime=min(SHUTDOWN_WAIT_SECONDS, 60), message=msg("daily_shutdown"))
                time.sleep(2)
                _start_server()
                start_time = time.time()
                next_scheduled_restart = compute_next_scheduled_restart()
                warning_keys_sent.clear()
                time.sleep(1)
                continue

            if ver == "Offline":
                status_var.set("Online")
                version_var.set(ver)
                players_var.set("0")
                update_player_list([])
            else:
                if not booted_message_printed:
                    boot_time = time.time() - boot_start_time if boot_start_time else 0
                    boot_str = (
                        f"{int(boot_time)}s"
                        if boot_time < 60
                        else f"{int(boot_time // 60)}m {int(boot_time % 60)}s"
                    )
                    log(f"Server is fully booted up, enjoy! Boot time: {boot_str}")
                    booted_message_printed = True
                count, names = get_online_players()
                players_var.set(count)
                update_player_list(names)
                version_var.set(ver)
                status_var.set("Online")

            if time.time() - last_checked_time >= 60:
                run_hourly_maintenance(save_file)
                last_checked_time = time.time()
        else:
            last_known_players = players_var.get()
            status_var.set("Offline")
            version_var.set("Offline")
            players_var.set("0")
            update_player_list([])
            save_stale_stacks = 0
            booted_message_printed = False
            boot_start_time = None
            warning_keys_sent.clear()
            if was_running:
                if manual_stop:
                    log("Server stopped manually.")
                    manual_stop = False
                    is_starting = False
                    root.after(0, lambda: (btn_start.config(state=tk.NORMAL), btn_stop.config(state=tk.DISABLED)))
                else:
                    if update_pending:
                        log_restart("update")
                        update_pending = False
                    else:
                        log(f"Server crashed! Restarting instantly... (players were {last_known_players})")
                    time.sleep(2)
                    _start_server()
                    start_time = time.time()
                was_running = False
        time.sleep(1)

# --- UI ------------------------------------------------------------------------

refresh_settings_from_ini()
next_scheduled_restart = compute_next_scheduled_restart()

root = tk.Tk()
status_var = StringVar(value="Idle")
version_var = StringVar(value="Offline")
players_var = StringVar(value="0")

try:
    root.iconbitmap(os.path.join(SCRIPT_DIR, "pal.ico"))
except Exception:
    pass

root.title("Palworld Server Manager")
root.geometry("1000x640")
apply_dark_theme(root)

main_frame = ttk.Frame(root)
main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

left_frame = ttk.Frame(main_frame)
left_frame.pack(side=tk.LEFT, fill=tk.BOTH, padx=(0, 15))

btn_frame = ttk.Frame(left_frame)
btn_frame.pack(anchor="w", pady=(0, 6), fill=tk.X)
btn_start = ttk.Button(btn_frame, text="Start Server", command=start_server, style="Dark.TButton")
btn_start.pack(fill=tk.X, pady=3)
btn_stop = ttk.Button(btn_frame, text="Stop Server", command=stop_server, style="Dark.TButton")
btn_stop.pack(fill=tk.X, pady=3)

info_frame = ttk.Frame(left_frame)
info_frame.pack(anchor="w", pady=(6, 4), fill=tk.X)
ttk.Label(info_frame, text="Daily restart:", style="TLabel").grid(row=0, column=0, sticky="e", padx=5)
ttk.Label(info_frame, text=RESTART_TIME, style="TLabel").grid(row=0, column=1, sticky="w", padx=5)

stats_frame = ttk.Frame(left_frame)
stats_frame.pack(anchor="w", pady=(4, 0), fill=tk.X)
for i, (label, var) in enumerate([
    ("Status:", status_var),
    ("Version:", version_var),
    ("Players:", players_var),
]):
    ttk.Label(stats_frame, text=label, style="TLabel").grid(row=i, column=0, sticky="e", padx=5, pady=2)
    ttk.Label(stats_frame, textvariable=var, style="TLabel").grid(row=i, column=1, sticky="w", padx=5, pady=2)

ttk.Label(left_frame, text="Online players", style="TLabel").pack(anchor="w", pady=(10, 2))
player_list_frame = ttk.Frame(left_frame)
player_list_frame.pack(fill=tk.BOTH, expand=True)
player_scroll = ttk.Scrollbar(player_list_frame, orient=tk.VERTICAL)
player_listbox = tk.Listbox(
    player_list_frame,
    bg="#333333",
    fg="white",
    selectbackground="#555555",
    selectforeground="white",
    activestyle="none",
    highlightthickness=0,
    borderwidth=0,
    font=("Arial", 10),
    yscrollcommand=player_scroll.set,
    exportselection=False,
)
player_scroll.config(command=player_listbox.yview)
player_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
player_scroll.pack(side=tk.RIGHT, fill=tk.Y)

right_frame = ttk.Frame(main_frame)
right_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
ttk.Label(right_frame, text="Log", style="TLabel").pack(anchor="w")
log_box = scrolledtext.ScrolledText(
    right_frame, wrap=tk.WORD, bg="#333333", fg="white", font=("Arial", 10), state=tk.DISABLED,
)
log_box.pack(fill=tk.BOTH, expand=True, pady=(2, 0))

def set_controls_state(running):
    btn_start.config(state=tk.DISABLED if running or is_starting else tk.NORMAL)
    btn_stop.config(state=tk.NORMAL if running or is_starting else tk.DISABLED)

def on_closing():
    stop_server()
    root.destroy()

def show_no_install_prompt():
    if is_server_installed():
        return
    text = msg("no_install")
    log(f"No server install found at {TARGET_EXE}")
    log(text)
    messagebox.showinfo("Palworld Server Manager", text, parent=root)

def run_startup_checks():
    show_no_install_prompt()
    if is_server_installed():
        check_palworld_settings()

root.protocol("WM_DELETE_WINDOW", on_closing)
threading.Thread(target=monitor_server, daemon=True).start()
root.after(200, run_startup_checks)
root.mainloop()
