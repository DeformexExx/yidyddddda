import asyncio
import html
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import telebot
from loguru import logger
from telebot import util
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
PROJECT_ROOT = "/data/data/com.termux/files/home/aegis_watchdog"
CONFIG_PATH = Path(PROJECT_ROOT) / "config.json"
DB_PATH = "/data/data/com.roblox.client/app_webview/Default/Cookies"
PID_FILE = Path(".bot.pid")
LOG_FILE = "watchdog.log"

@dataclass
class AppConfig:
    device_name: str
    bot_token: str
    admin_ids: list[int]

DEFAULT_CONFIG = {
    "DEVICE_NAME": "DEV_1",
    "BOT_TOKEN": "",
    "ADMIN_IDS": [],
}

@dataclass
class DeviceState:
    name: str
    pid: int
    output_on: bool = True
    last_status: str = "IDLE"

@dataclass
class SessionState:
    selected_device: str
    view: str = "main"
    message_id: int | None = None
    wait_mode: str | None = None  # None | WAIT_COOKIE | WAIT_SERVER

# Global Variables
sessions: dict[int, SessionState] = {}
tracked_messages: dict[int, tuple[int, int]] = {}
devices: dict[str, DeviceState] = {}
silent_mode = False

# ==========================================
# CORE UTILS & SHELL
# ==========================================

def load_config() -> AppConfig:
    try:
        if not CONFIG_PATH.exists():
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return AppConfig(
            device_name=str(raw.get("DEVICE_NAME", "DEV_1")),
            bot_token=str(raw.get("BOT_TOKEN", "")).strip(),
            admin_ids=[int(x) for x in raw.get("ADMIN_IDS", [])]
        )
    except Exception as e:
        print(f"CRITICAL: Failed to load config: {e}")
        sys.exit(1)

config = load_config()
bot = telebot.TeleBot(config.bot_token, parse_mode="HTML")

def run_shell(command: str, root: bool = True) -> tuple[int, str, str]:
    """Execute shell command via subprocess, optionally using su -c."""
    try:
        # Wrap command for su -c if root is requested
        if root:
            full_cmd = f"su -c {shlex.quote(command)}"
        else:
            full_cmd = command
            
        process = subprocess.run(
            full_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=120
        )
        return process.returncode, process.stdout.strip(), process.stderr.strip()
    except Exception as e:
        return -1, "", str(e)

# ==========================================
# DATABASE INJECTION (ROOT BYPASS)
# ==========================================

def db_exec(sql: str) -> tuple[int, str, str]:
    """Execute SQL directly via sqlite3 binary with root privileges."""
    # Use the requested format: su -c "sqlite3 /path/to/db \"{sql}\""
    # We must be careful with nested quotes.
    escaped_sql = sql.replace('"', '\\"')
    cmd = f'sqlite3 {DB_PATH} "{escaped_sql}"'
    return run_shell(cmd, root=True)

def get_db_schema() -> list[dict]:
    """Fetch table schema to handle dynamic NOT NULL columns."""
    code, out, err = db_exec("PRAGMA table_info(cookies);")
    columns = []
    if code == 0 and out:
        for line in out.splitlines():
            # Format: id|name|type|notnull|dflt_value|pk
            parts = line.split("|")
            if len(parts) >= 6:
                columns.append({
                    "name": parts[1],
                    "type": parts[2].upper(),
                    "notnull": int(parts[3]),
                    "dflt": parts[4]
                })
    return columns

def inject_cookie(cookie_value: str) -> tuple[bool, str]:
    """Inject Roblox cookie using the MONOLITH root method."""
    try:
        # 1. Stop Roblox to unlock DB
        run_shell("am force-stop com.roblox.client", root=True)
        time.sleep(1)

        # 2. Analyze Schema
        cols = get_db_schema()
        if not cols:
            return False, "Failed to read cookie schema (DB may be missing or locked)"

        # 3. Build Injection SQL
        creation_utc = "((strftime('%s','now') + 11644473600) * 1000000)"
        expires_utc = "((strftime('%s','now') + 11644473600 + 31536000) * 1000000)"
        esc_cookie = cookie_value.strip().replace("'", "''")
        
        provided = {
            "creation_utc": creation_utc,
            "host_key": "'.roblox.com'",
            "name": "'.ROBLOSECURITY'",
            "value": f"'{esc_cookie}'",
            "path": "'/'",
            "expires_utc": expires_utc,
            "is_secure": "1",
            "is_httponly": "1",
            "last_access_utc": creation_utc,
            "has_expires": "1",
            "is_persistent": "1",
            "priority": "1",
            "samesite": "-1",
            "source_port": "443",
            "last_update_utc": creation_utc,
            "source_scheme": "2",
        }

        insert_cols = []
        insert_vals = []
        for col in cols:
            name = col["name"]
            if name in provided:
                insert_cols.append(name)
                insert_vals.append(provided[name])
            elif col["notnull"]:
                # Fill mandatory columns with defaults
                if col["dflt"]:
                    insert_vals.append(col["dflt"])
                elif any(k in col["type"] for k in ("INT", "REAL", "NUM", "BOOL")):
                    insert_vals.append("0")
                else:
                    insert_vals.append("''")
                insert_cols.append(name)

        # 4. Execute Atomic Injection
        sql_blob = (
            "DELETE FROM cookies WHERE name = '.ROBLOSECURITY';"
            f"INSERT INTO cookies ({', '.join(insert_cols)}) VALUES ({', '.join(insert_vals)});"
            "REINDEX cookies;"
            "VACUUM;"
        )
        
        code, out, err = db_exec(sql_blob)
        if code != 0:
            return False, f"SQL Error: {err or out}"

        # 5. Fix Permissions & Restart
        run_shell(f"chmod 600 {DB_PATH}", root=True)
        run_shell("am start -n com.roblox.client/com.roblox.client.startup.ActivitySplash", root=True)
        
        return True, "Injection Successful (Root Bypass Used)"
    except Exception as e:
        return False, f"System Error: {str(e)}"

# ==========================================
# SYSTEM MONITORING
# ==========================================

def get_ram_usage() -> float:
    """Get RAM usage percentage with Kernel Glitch fallback."""
    try:
        # Use dumpsys for Android accuracy
        code, out, _ = run_shell("dumpsys meminfo | grep 'Total RAM:'", root=True)
        total_kb = 0
        free_kb = 0
        
        if code == 0 and out:
            match = re.search(r"Total RAM:\s+([\d,]+)K\s+\(([\d,]+)K\s+free\)", out)
            if match:
                total_kb = int(match.group(1).replace(",", ""))
                free_kb = int(match.group(2).replace(",", ""))

        if total_kb <= 0:
            # Fallback to /proc/meminfo
            code, out, _ = run_shell("cat /proc/meminfo", root=True)
            for line in out.splitlines():
                if line.startswith("MemTotal:"): total_kb = int(line.split()[1])
                if line.startswith("MemAvailable:"): free_kb = int(line.split()[1])

        # HARDCORE FALLBACK: Kernel Glitch Fix
        if total_kb > 64 * 1024 * 1024: # > 64GB
            total_kb = 4 * 1024 * 1024 # Force 4GB
            if free_kb > total_kb: free_kb = total_kb // 2

        if total_kb <= 0: return 0.0
        return ((total_kb - free_kb) / total_kb) * 100.0
    except:
        return 0.0

def get_cpu_usage() -> float:
    try:
        code, out, _ = run_shell("top -n 1 -b | grep com.roblox.client", root=True)
        if code == 0 and out:
            parts = out.split()
            for p in parts:
                if "%" in p: return float(p.replace("%", ""))
                if re.match(r"^\d+\.\d+$", p): return float(p)
        return 0.0
    except: return 0.0

def get_active_connections() -> int:
    try:
        code, out, _ = run_shell("cat /proc/net/tcp | grep ' 01 ' | wc -l", root=True)
        return int(out.strip()) if code == 0 else 0
    except: return 0

# ==========================================
# UI & HANDLERS
# ==========================================

def _bar(percent: float, length: int = 15) -> str:
    p = max(0.0, min(100.0, percent))
    fill = int((p / 100.0) * length)
    return "█" * fill + "░" * (length - fill)

def build_dashboard(selected_device: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    for dev_name in devices.keys():
        btn_text = f"📱 {dev_name}" + (" (Focus)" if dev_name == selected_device else "")
        kb.row(InlineKeyboardButton(btn_text, callback_data=f"dev:{dev_name}"))
    kb.row(InlineKeyboardButton("🚀 START ALL", callback_data="all:start"),
           InlineKeyboardButton("🛑 STOP ALL", callback_data="all:stop"))
    kb.row(InlineKeyboardButton("⚙️ SETTINGS", callback_data="menu:settings"))
    return kb

def build_device_page(name: str) -> tuple[str, InlineKeyboardMarkup]:
    ram = get_ram_usage()
    cpu = get_cpu_usage()
    con = get_active_connections()
    state = devices.get(name)
    
    text = (
        f"<pre>\n"
        f"DEVICE: {html.escape(name)}\n"
        f"RAM: [{_bar(ram)}] {ram:.1f}%\n"
        f"CPU: [{_bar(cpu)}] {cpu:.1f}%\n"
        f"CON: {con} | STATUS: {state.last_status if state else 'IDLE'}\n"
        f"WATCHDOG: {'ENABLED' if (state and state.output_on) else 'DISABLED'}\n"
        f"</pre>"
    )
    
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("▶️ START", callback_data=f"act:start:{name}"),
           InlineKeyboardButton("🛑 STOP", callback_data=f"act:stop:{name}"))
    kb.row(InlineKeyboardButton("🍪 COOKIE", callback_data=f"act:cookie:{name}"),
           InlineKeyboardButton("🔗 SERVER", callback_data=f"act:server:{name}"))
    kb.row(InlineKeyboardButton("⬅️ BACK", callback_data="menu:main"))
    return text, kb

@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    if message.from_user.id not in config.admin_ids: return
    user_id = message.from_user.id
    if user_id not in sessions:
        sessions[user_id] = SessionState(selected_device=next(iter(devices.keys()), config.device_name))
    
    state = sessions[user_id]
    state.view = "main"
    text = f"<pre>AEGIS MONOLITH HUB\nHOST: {config.device_name}\nFOCUS: {state.selected_device}</pre>"
    msg = bot.send_message(message.chat.id, text, reply_markup=build_dashboard(state.selected_device))
    tracked_messages[message.chat.id] = (user_id, msg.message_id)

@bot.callback_query_handler(func=lambda c: True)
def handle_callbacks(call):
    if call.from_user.id not in config.admin_ids: return
    user_id = call.from_user.id
    state = sessions.get(user_id)
    if not state: return

    data = call.data
    try:
        if data == "menu:main":
            state.view = "main"
            text = f"<pre>AEGIS MONOLITH HUB\nHOST: {config.device_name}\nFOCUS: {state.selected_device}</pre>"
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=build_dashboard(state.selected_device))
        
        elif data.startswith("dev:"):
            state.selected_device = data.split(":")[1]
            state.view = "device"
            text, kb = build_device_page(state.selected_device)
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)
        
        elif data.startswith("act:"):
            parts = data.split(":")
            act = parts[1]
            dev = parts[2]
            
            if act == "start":
                devices[dev].output_on = True
                devices[dev].last_status = "STARTING"
                # START COMMAND
                run_shell("am start -n com.roblox.client/com.roblox.client.startup.ActivitySplash", root=True)
                bot.answer_callback_query(call.id, f"{dev} started")
            elif act == "stop":
                devices[dev].output_on = False
                devices[dev].last_status = "STOPPED"
                run_shell("am force-stop com.roblox.client", root=True)
                bot.answer_callback_query(call.id, f"{dev} stopped")
            elif act == "cookie":
                state.wait_mode = "WAIT_COOKIE"
                bot.send_message(call.message.chat.id, "🍪 Send Roblox Cookie now:")
                bot.answer_callback_query(call.id)
            elif act == "server":
                state.wait_mode = "WAIT_SERVER"
                bot.send_message(call.message.chat.id, "🔗 Send Server Link now:")
                bot.answer_callback_query(call.id)
        
        elif data == "menu:settings":
            kb = InlineKeyboardMarkup()
            kb.row(InlineKeyboardButton("🔄 UPDATE", callback_data="set:update"))
            kb.row(InlineKeyboardButton(f"🔇 SILENT: {'ON' if silent_mode else 'OFF'}", callback_data="set:silent"))
            kb.row(InlineKeyboardButton("⬅️ BACK", callback_data="menu:main"))
            bot.edit_message_text("<pre>SYSTEM SETTINGS</pre>", call.message.chat.id, call.message.message_id, reply_markup=kb)
            
        elif data == "set:silent":
            global silent_mode
            silent_mode = not silent_mode
            bot.answer_callback_query(call.id, f"Silent mode {'ON' if silent_mode else 'OFF'}")
            # Refresh settings view
            kb = InlineKeyboardMarkup()
            kb.row(InlineKeyboardButton("🔄 UPDATE", callback_data="set:update"))
            kb.row(InlineKeyboardButton(f"🔇 SILENT: {'ON' if silent_mode else 'OFF'}", callback_data="set:silent"))
            kb.row(InlineKeyboardButton("⬅️ BACK", callback_data="menu:main"))
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=kb)

    except Exception as e:
        logger.error(f"Callback Error: {e}")
        bot.answer_callback_query(call.id, "Error occurred")

@bot.message_handler(func=lambda m: True)
def handle_text(message):
    if message.from_user.id not in config.admin_ids: return
    state = sessions.get(message.from_user.id)
    if not state or not state.wait_mode: return

    payload = message.text.strip()
    dev = state.selected_device
    
    if state.wait_mode == "WAIT_COOKIE":
        bot.send_message(message.chat.id, "⏳ Injecting cookie via Root Shell...")
        ok, res = inject_cookie(payload)
        bot.send_message(message.chat.id, f"{'✅' if ok else '❌'} {res}")
    
    elif state.wait_mode == "WAIT_SERVER":
        # WINNING LAUNCH COMMAND FORMAT
        link = payload.replace("'", "'\"'\"'")
        # Execute verified command via root shell bypass
        cmd = f"nohup am start -a android.intent.action.VIEW -d '{link}' com.roblox.client > /dev/null 2>&1 &"
        code, _, err = run_shell(cmd, root=True)
        bot.send_message(message.chat.id, "✅ Server link sent!" if code == 0 else f"❌ Error: {err}")

    state.wait_mode = None

# ==========================================
# MONITOR & WORKERS
# ==========================================

def monitor_worker():
    while True:
        try:
            con = get_active_connections()
            for dev in devices.values():
                if not dev.output_on: continue
                # Auto-restart if connections dropped too low
                if con < 5:
                    run_shell("am force-stop com.roblox.client && am start -n com.roblox.client/com.roblox.client.startup.ActivitySplash", root=True)
                    dev.last_status = "RESTARTED"
            time.sleep(30)
        except: time.sleep(30)

def ui_refresh_worker():
    while True:
        try:
            for chat_id, (user_id, msg_id) in list(tracked_messages.items()):
                state = sessions.get(user_id)
                if state and state.view == "device":
                    text, kb = build_device_page(state.selected_device)
                    try: bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb)
                    except: pass
            time.sleep(10)
        except: time.sleep(10)

# ==========================================
# MAIN ENTRY
# ==========================================

def main():
    print("Project MONOLITH v1.0 starting...")
    
    # 1. Startup Cleanup (Mandatory)
    run_shell("rm -f .bot.pid", root=True)
    
    # 2. File Initialization & Stabilization
    try:
        if not os.path.exists(LOG_FILE):
            open(LOG_FILE, "a").close()
        # Force 777 permissions
        run_shell(f"chmod 777 {LOG_FILE}", root=True)
        logger.add(LOG_FILE, rotation="10 MB", retention=3)
    except Exception as e:
        print(f"File Init Error: {e}")

    # 3. Discover Devices
    devices[config.device_name] = DeviceState(name=config.device_name, pid=os.getpid())
    for p in Path(".").glob("DEV_*.json"):
        if p.stem not in devices:
            devices[p.stem] = DeviceState(name=p.stem, pid=0)

    # 4. Start Threads
    threading.Thread(target=monitor_worker, daemon=True).start()
    threading.Thread(target=ui_refresh_worker, daemon=True).start()

    logger.info("Monolith Engine Online")

    # 5. Infinity Polling
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=40)
        except Exception as e:
            logger.error(f"Polling Crash: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
