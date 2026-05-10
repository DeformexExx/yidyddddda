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
    try:
        full_cmd = f"su -c {shlex.quote(command)}" if root else command
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
# 1. THE ONLY WORKING INJECTION METHOD (ROOT SHELL)
# ==========================================

def inject_cookie_root(cookie_value):
    # Total cleanup first to prevent duplicates
    cleanup_cmd = "sqlite3 /data/data/com.roblox.client/app_webview/Default/Cookies \"DELETE FROM cookies;\""
    
    # Direct SQL Injection via su -c
    insert_sql = f"""
    INSERT INTO cookies (creation_utc, host_key, name, value, path, expires_utc, is_secure, is_httponly, last_access_utc, has_expires, is_persistent, priority) 
    VALUES (strftime('%s','now'), '.roblox.com', '.ROBLOSECURITY', '{cookie_value}', '/', strftime('%s','now', '+1 year'), 1, 1, strftime('%s','now'), 1, 1, 1);
    """
    # Added a semicolon between cleanup and insert for shell execution
    full_cmd = f"su -c \"{cleanup_cmd}; sqlite3 /data/data/com.roblox.client/app_webview/Default/Cookies '{insert_sql}'\""
    
    import subprocess
    result = subprocess.run(full_cmd, shell=True, capture_output=True)
    
    # Relaunch Roblox
    run_shell("am start -n com.roblox.client/com.roblox.client.startup.ActivitySplash", root=True)
    
    if result.returncode == 0:
        return True, "Injection Successful (Root Shell)"
    return False, f"SQL Error: {result.stderr.decode('utf-8', errors='ignore')}"

# ==========================================
# 3. ACCURATE RAM FALLBACK
# ==========================================

def get_ram_display_stats() -> tuple[float, str]:
    """Get RAM stats with Glitch Fallback and Python Process Memory info."""
    try:
        code, out, _ = run_shell("dumpsys meminfo | grep 'Total RAM:'", root=True)
        total_kb = 0
        free_kb = 0
        
        if code == 0 and out:
            match = re.search(r"Total RAM:\s+([\d,]+)K\s+\(([\d,]+)K\s+free\)", out)
            if match:
                total_kb = int(match.group(1).replace(",", ""))
                free_kb = int(match.group(2).replace(",", ""))

        if total_kb <= 0:
            code, out, _ = run_shell("cat /proc/meminfo", root=True)
            for line in out.splitlines():
                if line.startswith("MemTotal:"): total_kb = int(line.split()[1])
                if line.startswith("MemAvailable:"): free_kb = int(line.split()[1])

        # Glitch Check (> 64GB)
        if total_kb > 64 * 1024 * 1024:
            # Hardcode display to 4GB
            display_total = "4.0 GB"
            # Get Python memory
            code_py, out_py, _ = run_shell(f"dumpsys meminfo {os.getpid()} | grep 'TOTAL PSS:'", root=True)
            py_mem = out_py.split()[2] if code_py == 0 and out_py else "Unknown"
            return 25.0, f"TOTAL: {display_total} (FIXED) | PY: {py_mem}KB"
        
        used_percent = ((total_kb - free_kb) / total_kb) * 100.0 if total_kb > 0 else 0.0
        return used_percent, f"{used_percent:.1f}% ({total_kb//1024}MB)"
    except:
        return 0.0, "Error"

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
    ram_p, ram_txt = get_ram_display_stats()
    cpu = get_cpu_usage()
    con = get_active_connections()
    state = devices.get(name)
    
    text = (
        f"<pre>\n"
        f"DEVICE: {html.escape(name)}\n"
        f"RAM: [{_bar(ram_p)}] {ram_txt}\n"
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

# 2. SYNTAX ERROR FIX (GLOBAL DECLARATION)
@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    global silent_mode
    if message.from_user.id not in config.admin_ids: return
    user_id = message.from_user.id
    if user_id not in sessions:
        sessions[user_id] = SessionState(selected_device=next(iter(devices.keys()), config.device_name))
    
    state = sessions[user_id]
    state.view = "main"
    text = f"<pre>AEGIS MONOLITH HUB\nHOST: {config.device_name}\nFOCUS: {state.selected_device}\nSILENT: {'ON' if silent_mode else 'OFF'}</pre>"
    msg = bot.send_message(message.chat.id, text, reply_markup=build_dashboard(state.selected_device))
    tracked_messages[message.chat.id] = (user_id, msg.message_id)

@bot.callback_query_handler(func=lambda c: True)
def handle_callbacks(call):
    global silent_mode
    if call.from_user.id not in config.admin_ids: return
    user_id = call.from_user.id
    state = sessions.get(user_id)
    if not state: return

    data = call.data
    try:
        if data == "menu:main":
            state.view = "main"
            text = f"<pre>AEGIS MONOLITH HUB\nHOST: {config.device_name}\nFOCUS: {state.selected_device}\nSILENT: {'ON' if silent_mode else 'OFF'}</pre>"
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=build_dashboard(state.selected_device))
        
        elif data.startswith("dev:"):
            state.selected_device = data.split(":")[1]
            state.view = "device"
            text, kb = build_device_page(state.selected_device)
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)
        
        elif data.startswith("act:"):
            parts = data.split(":")
            act, dev = parts[1], parts[2]
            if act == "start":
                devices[dev].output_on = True
                devices[dev].last_status = "STARTING"
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
            silent_mode = not silent_mode
            bot.answer_callback_query(call.id, f"Silent mode {'ON' if silent_mode else 'OFF'}")
            # Refresh settings view
            kb = InlineKeyboardMarkup()
            kb.row(InlineKeyboardButton("🔄 UPDATE", callback_data="set:update"))
            kb.row(InlineKeyboardButton(f"🔇 SILENT: {'ON' if silent_mode else 'OFF'}", callback_data="set:silent"))
            kb.row(InlineKeyboardButton("⬅️ BACK", callback_data="menu:main"))
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=kb)

        elif data == "set:update":
            bot.answer_callback_query(call.id, "Starting Update...")
            # 5. UPDATE MECHANISM
            update_cmd = "git -c safe.directory='*' fetch --all && git -c safe.directory='*' reset --hard origin/main && git -c safe.directory='*' clean -fd"
            code, out, err = run_shell(update_cmd, root=False)
            if code == 0:
                bot.send_message(call.message.chat.id, "✅ Update Successful. Restarting...")
                os._exit(0)
            else:
                bot.send_message(call.message.chat.id, f"❌ Update Failed: {err or out}")

    except Exception as e:
        logger.error(f"Callback Error: {e}")
        bot.answer_callback_query(call.id, "Error occurred")

@bot.message_handler(func=lambda m: True)
def handle_text(message):
    global silent_mode
    if message.from_user.id not in config.admin_ids: return
    state = sessions.get(message.from_user.id)
    if not state or not state.wait_mode: return

    payload = message.text.strip()
    
    if state.wait_mode == "WAIT_COOKIE":
        bot.send_message(message.chat.id, "⏳ Injecting cookie via Root Shell (STRICT)...")
        ok, res = inject_cookie_root(payload)
        bot.send_message(message.chat.id, f"{'✅' if ok else '❌'} {res}")
    
    elif state.wait_mode == "WAIT_SERVER":
        link = payload.replace("'", "'\"'\"'")
        # WINNING LAUNCH COMMAND
        cmd = f"su -c \"nohup am start -a android.intent.action.VIEW -d '{link}' com.roblox.client > /dev/null 2>&1 &\""
        code, _, err = run_shell(cmd, root=False)
        bot.send_message(message.chat.id, "✅ Server link sent!" if code == 0 else f"❌ Error: {err}")

    state.wait_mode = None

# ==========================================
# MONITOR & WORKERS
# ==========================================

def monitor_worker():
    global silent_mode
    while True:
        try:
            con = get_active_connections()
            for dev in devices.values():
                if not dev.output_on: continue
                if con < 5:
                    run_shell("am force-stop com.roblox.client && am start -n com.roblox.client/com.roblox.client.startup.ActivitySplash", root=True)
                    dev.last_status = "RESTARTED"
            time.sleep(30)
        except: time.sleep(30)

def ui_refresh_worker():
    global silent_mode
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
    # 4. RE-REGISTRATION OF CALLBACKS / HANDLERS (Implicit in Telebot)
    # Ensuring logger and stabilization before everything
    print("Project MONOLITH v1.1 - Deploying STRICT fixes...")
    
    # Root Cleanup
    run_shell("rm -f .bot.pid", root=True)
    
    # File Initialization
    try:
        if not os.path.exists(LOG_FILE):
            open(LOG_FILE, "a").close()
        run_shell(f"chmod 777 {LOG_FILE}", root=True)
        logger.add(LOG_FILE, rotation="10 MB", retention=3)
    except Exception as e:
        print(f"File Init Error: {e}")

    # Discover Devices
    devices[config.device_name] = DeviceState(name=config.device_name, pid=os.getpid())
    for p in Path(".").glob("DEV_*.json"):
        if p.stem not in devices:
            devices[p.stem] = DeviceState(name=p.stem, pid=0)

    # Start Threads
    threading.Thread(target=monitor_worker, daemon=True).start()
    threading.Thread(target=ui_refresh_worker, daemon=True).start()

    logger.info("Monolith v1.1 STRICT Online")

    # Infinity Polling
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=40)
        except Exception as e:
            logger.error(f"Polling Crash: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
