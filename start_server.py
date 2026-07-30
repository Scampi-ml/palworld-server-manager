import os, sys, time, shutil, zipfile, base64, json, subprocess, threading, urllib.request, tkinter as tk, re, socket
from tkinter import ttk, scrolledtext, StringVar
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
CONFIG_EXAMPLE_PATH = os.path.join(SCRIPT_DIR, "config.example.json")
STEAMCMD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
game_app_id = "2394010"
username = "admin"

def resolve_path(value):
    if not value:
        return value
    if os.path.isabs(value):
        return value
    return os.path.normpath(os.path.join(SCRIPT_DIR, value))

def load_config():
    if not os.path.isfile(CONFIG_PATH):
        if os.path.isfile(CONFIG_EXAMPLE_PATH):
            shutil.copy2(CONFIG_EXAMPLE_PATH, CONFIG_PATH)
        else:
            raise FileNotFoundError("Missing config.json and config.example.json")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

CONFIG = load_config()
PATHS = CONFIG.get("paths", {})

STEAMCMD_DIR = resolve_path(PATHS.get("steamcmd_dir", "steamcmd"))
STEAMCMD_EXE = os.path.join(STEAMCMD_DIR, "steamcmd.exe")
STEAMCMD_ZIP = os.path.join(SCRIPT_DIR, "steamcmd.zip")
INSTALL_DIR = resolve_path(PATHS.get("install_dir", "server"))
TARGET_EXE = resolve_path(PATHS.get("server_exe", "server/PalServer.exe"))
SHIPPING_EXE = resolve_path(PATHS.get(
    "shipping_exe",
    "server/Pal/Binaries/Win64/PalServer-Win64-Shipping-Cmd.exe"
))
TARGET_DIR = os.path.dirname(TARGET_EXE)
SERVER_ID_FILE = resolve_path(PATHS.get(
    "server_id_file",
    "server/Pal/Saved/Config/WindowsServer/GameUserSettings.ini"
))
SERVER_SETTINGS_FILE = resolve_path(PATHS.get(
    "settings_file",
    "server/Pal/Saved/Config/WindowsServer/PalWorldSettings.ini"
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
}
MESSAGES = {**DEFAULT_MESSAGES, **(CONFIG.get("messages") or {})}

def msg(key, **kwargs):
    text = MESSAGES.get(key, DEFAULT_MESSAGES.get(key, key))
    try:
        return text.format(**kwargs) if kwargs else text
    except (KeyError, ValueError):
        return text

start_thread = None
is_starting = False
manual_stop = False
start_time = time.time()
update_pending = False
restart_events = []
last_checked_minute = None
save_stale_stacks = 0
server_restapi_port = None
admin_password = None
SERVER_PORT = None
next_scheduled_restart = None

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
        k, v = part.split("=", 1)
        if k.strip() == keyword:
            return v.strip().strip('"')
    return None

def refresh_settings_from_ini():
    global server_restapi_port, admin_password, SERVER_PORT
    server_restapi_port = get_setting_value(SERVER_SETTINGS_FILE, "RESTAPIPort")
    admin_password = get_setting_value(SERVER_SETTINGS_FILE, "AdminPassword")
    SERVER_PORT = get_setting_value(SERVER_SETTINGS_FILE, "PublicPort")

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

def base64_auth_info():
    password = admin_password or ""
    return base64.b64encode(f"{username}:{password}".encode("ascii")).decode("ascii")

def api_url(path):
    return f"http://{server_address}:{server_restapi_port}{path}"

def api_get(path, timeout=5):
    if not server_restapi_port or admin_password is None:
        return None
    try:
        req = urllib.request.Request(api_url(path))
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Basic {base64_auth_info()}")
        req.add_header("User-Agent", "PalworldServerManager")
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                body = response.read().decode()
                return json.loads(body) if body else {}
    except Exception:
        return None
    return None

def api_post(path, body=None, timeout=10):
    if not server_restapi_port or admin_password is None:
        return False
    try:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(api_url(path), data=data, method="POST")
        req.add_header("Authorization", f"Basic {base64_auth_info()}")
        req.add_header("User-Agent", "PalworldServerManager")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status == 200
    except (HTTPError, URLError, TimeoutError, OSError):
        return False

def get_server_version_from_api():
    data = api_get("/v1/api/info")
    if data and "version" in data:
        return ".".join(data["version"].split(".")[:3])
    return "Offline"

def get_players_info():
    data = api_get("/v1/api/players")
    if data is None:
        return None
    players = data.get("players", data if isinstance(data, list) else [])
    if not isinstance(players, list):
        players = []
    names = []
    for player in players:
        if isinstance(player, dict):
            names.append(player.get("name") or player.get("playerName") or player.get("accountName") or "Unknown")
        else:
            names.append(str(player))
    return {"count": str(len(players)), "names": names}

def send_server_announcement(message):
    if api_post("/v1/api/announce", {"message": message}):
        log(f"Announcement sent: {message}")
    else:
        log(f"Failed to send announcement: {message}")

def save_world():
    ok = api_post("/v1/api/save")
    if ok:
        log("World save requested via REST API.")
    else:
        log("World save via REST API failed.")
    return ok

def request_shutdown(waittime, message):
    ok = api_post("/v1/api/shutdown", {"waittime": int(waittime), "message": message})
    if ok:
        log(f"Graceful shutdown requested ({waittime}s): {message}")
    else:
        log("Graceful shutdown via REST API failed.")
    return ok

def request_force_stop():
    ok = api_post("/v1/api/stop")
    if ok:
        log("Force stop requested via REST API.")
    else:
        log("Force stop via REST API failed.")
    return ok

def apply_dark_theme(window):
    font_style = ("Arial", 10)
    style = ttk.Style(window)
    style.theme_use("clam")
    style.configure("Dark.TButton", background="#555555", foreground="white", font=font_style, padding=6)
    style.map("Dark.TButton", background=[("active", "#666666"), ("!disabled", "#555555")])
    style.configure("TFrame", background="#2f2f2f")
    style.configure("TLabel", background="#2f2f2f", foreground="white")
    style.configure("Treeview", background="#333333", foreground="white", fieldbackground="#333333", borderwidth=0)
    style.configure("Treeview.Heading", background="#444444", foreground="white", font=("Arial", 10, "bold"))
    style.configure("TEntry", fieldbackground="#444444", foreground="white")
    window.configure(bg="#2f2f2f")

def update_status(msg):
    status_var.set(msg)

def update_version(msg):
    version_var.set(msg)

def update_next_restart_label():
    if next_scheduled_restart is None:
        next_restart_var.set(RESTART_TIME)
    else:
        next_restart_var.set(next_scheduled_restart.strftime("%Y-%m-%d %H:%M"))

def log(msg):
    now = datetime.now().strftime("[%I:%M:%S%p]").lstrip("0")
    message = f"{now} {msg}"
    try:
        log_box.config(state=tk.NORMAL)
        log_box.insert(tk.END, message + "\n")
        log_box.see(tk.END)
        log_box.config(state=tk.DISABLED)
    except Exception:
        pass
    os.makedirs(LOG_DIR, exist_ok=True)
    datestamp = datetime.now().strftime("%Y-%m-%d")
    with open(os.path.join(LOG_DIR, f"server_{datestamp}.log"), "a", encoding="utf-8") as f:
        f.write(message + "\n")

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
    cmd = [STEAMCMD_EXE, "+login", "anonymous", "+quit"]
    log("SteamCMD initialization started...")
    with open(init_log, "w", encoding="utf-8") as out:
        subprocess.run(cmd, text=True, stdout=out, stderr=out)
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
    appinfo_file = os.path.join(appinfo_dir, f"appmanifest_{game_app_id}.acf")
    steamcmd_cmd = [
        STEAMCMD_EXE, "+login", "anonymous", "+app_info_update", str(game_app_id),
        "+app_info_print", str(game_app_id), "+logoff", "+quit"
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
        server_was_running = is_server_running()
        if first_install:
            log("First install detected. Downloading dedicated server files...")
            log("SteamCMD download in progress (this often takes several minutes). Please wait...")
        else:
            log("Server update detected, starting update...")
            if server_was_running:
                send_server_announcement(msg("update_announce"))
                update_pending = True
                graceful_stop(waittime=30, message=msg("update_shutdown"), force=True)
            else:
                log("Server is not running; skipping announce/shutdown before update.")
            log("SteamCMD update download in progress (this often takes several minutes). Please wait...")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        update_dir = os.path.join(SCRIPT_DIR, "server_updates", f"Build_{buildid}_{timestamp}")
        os.makedirs(update_dir, exist_ok=True)
        steamcmd_log = os.path.join(LOG_DIR, "steamcmd_update.log")
        os.makedirs(LOG_DIR, exist_ok=True)
        cmd = [
            STEAMCMD_EXE, "+login", "anonymous",
            "+force_install_dir", update_dir,
            "+app_update", game_app_id, "validate", "+quit"
        ]
        with open(steamcmd_log, "w", encoding="utf-8") as out:
            subprocess.run(cmd, stdout=out, stderr=out)
        log(f"Download finished. Files staged at {update_dir}")
        log("Copying files into the server install directory...")
        for item in os.listdir(update_dir):
            s = os.path.join(update_dir, item)
            d = os.path.join(INSTALL_DIR, item)
            if os.path.isdir(s):
                shutil.copytree(
                    s, d, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("Engine.ini", "PalWorldSettings.ini")
                )
            else:
                shutil.copy2(s, d)
        if os.path.isfile(ENGINE_INI):
            dest_engine = os.path.join(
                INSTALL_DIR, "Pal", "Saved", "Config", "WindowsServer", "Engine.ini"
            )
            os.makedirs(os.path.dirname(dest_engine), exist_ok=True)
            shutil.copy2(ENGINE_INI, dest_engine)
        if first_install:
            log("First install complete.")
        else:
            log("Update files copied to main install directory.")
        if not os.path.isfile(TARGET_EXE):
            log(f"WARNING: Expected server executable missing after install: {TARGET_EXE}")
            log("Check paths.server_exe in config.json (usually server/PalServer.exe).")
    else:
        log("No update detected.")
    if os.path.exists(buildid_output_file):
        os.remove(buildid_output_file)

def perform_backup():
    datestamp = datetime.now().strftime("%Y-%m-%d")
    hourstamp = datetime.now().strftime("%H")
    daily_folder = os.path.join(BACKUP_DIR, f"Backup_{datestamp}")
    os.makedirs(daily_folder, exist_ok=True)
    backup_file = os.path.join(daily_folder, f"Backup_{hourstamp}.zip")
    if not os.path.exists(backup_file):
        log("Starting backup...")
        try:
            with zipfile.ZipFile(backup_file, "w", zipfile.ZIP_DEFLATED) as backup_zip:
                for foldername, _subfolders, filenames in os.walk(BACKUP_PATH):
                    for filename in filenames:
                        filepath = os.path.join(foldername, filename)
                        arcname = os.path.join(
                            os.path.basename(BACKUP_PATH),
                            os.path.relpath(filepath, BACKUP_PATH)
                        )
                        backup_zip.write(filepath, arcname)
            log(f"Backup completed: Backup_{datestamp}/Backup_{hourstamp}.zip")
        except Exception as e:
            log(f"Backup failed: {e}")

def wait_for_process_exit(timeout_seconds):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not is_server_running():
            return True
        time.sleep(1)
    return not is_server_running()

def kill_server_processes():
    for path in (SHIPPING_EXE, TARGET_EXE):
        kill_process(path)

def graceful_stop(waittime=None, message=None, force=False):
    waittime = SHUTDOWN_WAIT_SECONDS if waittime is None else waittime
    if message is None:
        message = msg("default_shutdown")
    refresh_settings_from_ini()
    if not is_server_running():
        log("Server is not running; nothing to stop.")
        return True
    if get_server_version_from_api() != "Offline":
        save_world()
        if request_shutdown(waittime, message):
            if wait_for_process_exit(waittime + 30):
                log("Server stopped gracefully.")
                return True
        if request_force_stop():
            if wait_for_process_exit(15):
                log("Server stopped via REST force stop.")
                return True
    if force or is_server_running():
        log("Falling back to process kill...")
        kill_server_processes()
        wait_for_process_exit(10)
    return not is_server_running()

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
    if is_server_running():
        kill_server_processes()
    download_and_extract_steamcmd()
    check_update()
    if not SERVER_PORT:
        log("PublicPort missing from PalWorldSettings.ini; using 8211.")
        port = "8211"
    else:
        port = SERVER_PORT
    server_query_port = get_free_query_port(server_query_port)
    log(f"Using QueryPort {server_query_port}")
    log("Starting server, please wait...")
    if not os.path.isfile(TARGET_EXE):
        log(f"Server executable not found: {TARGET_EXE}")
        root.after(0, lambda: (btn_start.config(state=tk.NORMAL), btn_stop.config(state=tk.DISABLED)))
        return
    cmd = [
        TARGET_EXE,
        "-useperfthreads",
        "-UseMultithreadForDS",
        f"-port={port}",
        f"-QueryPort={server_query_port}",
    ]
    subprocess.Popen(cmd)

def stop_server():
    global manual_stop, is_starting
    manual_stop = True
    is_starting = False
    btn_stop.config(state=tk.DISABLED)
    btn_start.config(state=tk.DISABLED)
    log("Stopping server...")
    threading.Thread(
        target=lambda: graceful_stop(message=msg("manual_stop")),
        daemon=True
    ).start()

def trim_ram_usage(exe_path):
    exe_name = os.path.basename(exe_path)
    exe_dir = os.path.dirname(os.path.abspath(exe_path)).lower()
    ps_script = f"""
    Add-Type -TypeDefinition @"
    using System;
    using System.Runtime.InteropServices;
    public class Win32 {{
        [DllImport("psapi.dll")]
        public static extern bool EmptyWorkingSet(IntPtr hProcess);
    }}
"@
    function Format-Size([double]$sizeMB) {{
        if ($sizeMB -ge 1024) {{
            return '{{0:N2}} GB' -f ($sizeMB / 1024)
        }} else {{
            return '{{0:N2}} MB' -f $sizeMB
        }}
    }}
    $output = @()
    $processes = Get-Process -Name '{os.path.splitext(exe_name)[0]}' -ErrorAction SilentlyContinue
    $found = $false
    foreach ($proc in $processes) {{
        try {{
            $path = ($proc.Path) -replace '\\\\', '/'
            if ($path -and $path.ToLower().StartsWith("{exe_dir.replace(chr(92), '/')}")) {{
                $found = $true
                $beforeMB = $proc.WorkingSet64 / 1MB
                [Win32]::EmptyWorkingSet($proc.Handle) | Out-Null
                Start-Sleep -Seconds 1
                $proc.Refresh()
                $afterMB = $proc.WorkingSet64 / 1MB
                $beforeFormatted = Format-Size $beforeMB
                $afterFormatted = Format-Size $afterMB
                $output += "Trimmed RAM for PID $($proc.Id): $beforeFormatted -> $afterFormatted"
            }}
        }} catch {{
            $output += "Error processing PID $($proc.Id): $($_.Exception.Message)"
        }}
    }}
    if (-not $found) {{
        $output += "No matching process in specified directory found."
    }}
    $output -join "`n"
    """
    completed = subprocess.run(["powershell", "-Command", ps_script], capture_output=True, text=True)
    out = completed.stdout.strip()
    log(out if out else "No output from PowerShell script.")

def add_event(line):
    restart_events.insert(0, line)
    if len(restart_events) > 100:
        restart_events.pop()
    try:
        root.after(0, refresh_event_list)
    except Exception:
        pass
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "events.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")

def log_restart(reason):
    now = datetime.now().strftime("%I:%M:%S%p").lstrip("0")
    players = players_var.get()
    log(f"========== RESTART: {reason} ==========")
    log(f"Time: {now} | Players: {players} | Next daily restart: {RESTART_TIME}")
    add_event(f"[{now}] {reason.replace('_', ' ').title()} | P:{players}")

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
    for m in range(5, 0, -1):
        key = f"{m}m"
        if 0 < time_to_restart <= m * 60 and key not in warning_keys_sent:
            send_server_announcement(msg("restart_minutes", minutes=m))
            warning_keys_sent.add(key)
    if 0 < time_to_restart <= 60 and "final" not in warning_keys_sent:
        send_server_announcement(msg("restart_now"))
        warning_keys_sent.add("final")

def monitor_server():
    global start_time, save_stale_stacks, manual_stop, update_pending, next_scheduled_restart, is_starting
    was_running = False
    last_checked_time = 0
    booted_message_printed = False
    boot_start_time = None
    warning_keys_sent = set()
    next_scheduled_restart = compute_next_scheduled_restart()
    root.after(0, update_next_restart_label)
    while True:
        running = is_server_running()
        root.after(0, set_controls_state, running)
        root.after(0, update_next_restart_label)
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
            if ver == "Offline":
                update_status("Online")
                update_version(ver)
                players_var.set("0")
                time.sleep(1)
                continue
            if not booted_message_printed:
                boot_time = time.time() - boot_start_time if boot_start_time else 0
                boot_time_str = f"{int(boot_time)}s" if boot_time < 60 else f"{int(boot_time // 60)}m {int(boot_time % 60)}s"
                log(f"Server is fully booted up, enjoy! Boot time: {boot_time_str}")
                booted_message_printed = True
            players = get_players_info()
            if players:
                players_var.set(players["count"])
            else:
                metrics = api_get("/v1/api/metrics")
                players_var.set(str(metrics.get("currentplayernum", 0)) if metrics else "0")
            update_version(ver)
            update_status("Online")
            time_to_restart = seconds_until_scheduled_restart()
            if time_to_restart is not None:
                maybe_send_restart_warnings(time_to_restart, warning_keys_sent)
            if time.time() - last_checked_time >= 60:
                check_update()
                perform_backup()
                trim_ram_usage(SHIPPING_EXE if os.path.isfile(SHIPPING_EXE) else TARGET_EXE)
                last_checked_time = time.time()
                if os.path.isfile(save_file):
                    age_minutes = (time.time() - os.path.getmtime(save_file)) / 60
                    if age_minutes >= MAX_SAVE_AGE_MINUTES:
                        save_stale_stacks += 1
                        log(f"Save file stale for {age_minutes:.1f} mins, stack {save_stale_stacks}/5")
                        if save_stale_stacks >= 5:
                            send_server_announcement(msg("stale_save_announce"))
                            log_restart("stale_save")
                            graceful_stop(waittime=30, message=msg("stale_save_shutdown"), force=True)
                            time.sleep(2)
                            _start_server()
                            start_time = time.time()
                            save_stale_stacks = 0
                            warning_keys_sent.clear()
                    else:
                        save_stale_stacks = 0
            if time_to_restart is not None and time_to_restart <= 0:
                log_restart("scheduled")
                graceful_stop(
                    waittime=min(SHUTDOWN_WAIT_SECONDS, 60),
                    message=msg("daily_shutdown"),
                    force=True
                )
                time.sleep(2)
                _start_server()
                start_time = time.time()
                next_scheduled_restart = compute_next_scheduled_restart()
                warning_keys_sent.clear()
                root.after(0, update_next_restart_label)
        else:
            last_known_players = players_var.get()
            update_status("Offline")
            update_version("Offline")
            players_var.set("0")
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
                    now = datetime.now().strftime("%I:%M:%S%p").lstrip("0")
                    add_event(f"[{now}] Manual Stop | P:{last_known_players}")
                else:
                    if update_pending:
                        log_restart("update")
                        update_pending = False
                    else:
                        log("Server crashed! Restarting instantly...")
                        now = datetime.now().strftime("%I:%M:%S%p").lstrip("0")
                        add_event(f"[{now}] Crash | P:{last_known_players}")
                    time.sleep(2)
                    _start_server()
                    start_time = time.time()
                was_running = False
        time.sleep(1)

def get_process_id(executable_name, target_path):
    try:
        ps_cmd = f"Get-Process -Name '{os.path.splitext(executable_name)[0]}' -ErrorAction SilentlyContinue | Select-Object Id,Path"
        result = subprocess.run(["powershell", "-Command", ps_cmd], capture_output=True, text=True)
        lines = [
            l.strip() for l in result.stdout.splitlines()
            if l.strip() and not l.startswith("Id") and not l.startswith("--")
        ]
        target_norm = os.path.normcase(os.path.abspath(target_path))
        script_norm = os.path.normcase(SCRIPT_DIR)
        for line in lines:
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                continue
            pid, exe_path = parts[0], parts[1]
            exe_norm = os.path.normcase(exe_path)
            if target_norm in exe_norm or script_norm in exe_norm:
                return pid
        return None
    except Exception as e:
        print(f"DEBUG: Error -> {e}")
        return None

def is_process_running(path):
    exe_name = os.path.basename(path)
    return bool(get_process_id(exe_name, path))

def is_server_running():
    if is_process_running(SHIPPING_EXE):
        return True
    return is_process_running(TARGET_EXE)

def kill_process(path):
    exe_name = os.path.basename(path)
    pid = get_process_id(exe_name, path)
    if pid:
        subprocess.run(["taskkill", "/F", "/PID", pid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

refresh_settings_from_ini()
next_scheduled_restart = compute_next_scheduled_restart()

root = tk.Tk()
status_var = StringVar(value="Idle")
version_var = StringVar(value="Offline")
players_var = StringVar(value="0")
next_restart_var = StringVar(value=RESTART_TIME)

try:
    root.iconbitmap(os.path.join(SCRIPT_DIR, "pal.ico"))
except Exception:
    pass

root.title("Palworld Server Manager")
root.geometry("950x520")
apply_dark_theme(root)

main_frame = ttk.Frame(root)
main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

left_frame = ttk.Frame(main_frame)
left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 15))

btn_frame = ttk.Frame(left_frame)
btn_frame.pack(anchor="w", pady=(0, 10))
btn_start = ttk.Button(btn_frame, text="Start Server", command=start_server, style="Dark.TButton")
btn_start.pack(fill=tk.X, pady=3)
btn_stop = ttk.Button(btn_frame, text="Stop Server", command=stop_server, style="Dark.TButton")
btn_stop.pack(fill=tk.X, pady=3)

info_frame = ttk.Frame(left_frame)
info_frame.pack(anchor="w", pady=(10, 10))
ttk.Label(info_frame, text="Daily restart:", style="TLabel").grid(row=0, column=0, sticky="e", padx=5)
ttk.Label(info_frame, text=RESTART_TIME, style="TLabel").grid(row=0, column=1, sticky="w", padx=5)
ttk.Label(info_frame, text="(from config.json)", style="TLabel").grid(row=1, column=0, columnspan=2, sticky="w", padx=5)

right_frame = ttk.Frame(main_frame)
right_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

stats_frame = ttk.Frame(right_frame)
stats_frame.pack(anchor="n", pady=(0, 10))

labels = [
    ("Status:", status_var),
    ("Version:", version_var),
    ("Players:", players_var),
    ("Next restart:", next_restart_var),
]
for i, (label, var) in enumerate(labels):
    ttk.Label(stats_frame, text=label, style="TLabel").grid(row=i // 2, column=(i % 2) * 2, sticky="e", padx=5, pady=2)
    ttk.Label(stats_frame, textvariable=var, style="TLabel").grid(row=i // 2, column=(i % 2) * 2 + 1, sticky="w", padx=5, pady=2)

log_frame = ttk.Frame(right_frame)
log_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
log_frame.grid_columnconfigure(0, weight=1)
log_frame.grid_columnconfigure(1, weight=1)
log_frame.grid_rowconfigure(0, weight=1)

log_pane = ttk.Frame(log_frame)
log_pane.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
ttk.Label(log_pane, text="Log", style="TLabel").pack(anchor="w")
log_box = scrolledtext.ScrolledText(log_pane, wrap=tk.WORD, bg="#333333", fg="white",
                                    font=("Arial", 10), state=tk.DISABLED)
log_box.pack(fill=tk.BOTH, expand=True, pady=(2, 0))

event_pane = ttk.Frame(log_frame)
event_pane.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
ttk.Label(event_pane, text="Events", style="TLabel").pack(anchor="w")
event_text = scrolledtext.ScrolledText(event_pane, wrap=tk.WORD, bg="#333333", fg="white",
                                       font=("Consolas", 9), state=tk.DISABLED)
event_text.pack(fill=tk.BOTH, expand=True, pady=(2, 0))

def refresh_event_list():
    event_text.config(state=tk.NORMAL)
    event_text.delete("1.0", tk.END)
    for ev in restart_events[:50]:
        event_text.insert(tk.END, ev + "\n")
    event_text.config(state=tk.DISABLED)

def set_controls_state(running):
    btn_start.config(state=tk.DISABLED if running or is_starting else tk.NORMAL)
    btn_stop.config(state=tk.NORMAL if running or is_starting else tk.DISABLED)

def on_closing():
    stop_server()
    root.destroy()

root.protocol("WM_DELETE_WINDOW", on_closing)

threading.Thread(target=monitor_server, daemon=True).start()
root.mainloop()
