import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import telebot
from telebot import types
from loguru import logger

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
PROJECT_ROOT = "/data/data/com.termux/files/home/aegis_watchdog"
CONFIG_PATH = Path(PROJECT_ROOT) / "config.json"
DB_PATH = "/data/data/com.roblox.client/app_webview/Default/Cookies"
SQLITE_BIN = "/data/data/com.termux/files/usr/bin/sqlite3"
LOG_FILE = "watchdog.log"
SCREENSHOT_PATH = "/sdcard/screen.png"

# Global State
silent_mode = False
admin_ids = []
device_name = "DEV_1"
server_link = ""

# ==========================================
# CORE UTILS & SHELL (ROOT ONLY)
# ==========================================

def run_root(command: str) -> tuple[int, str, str]:
    """Execute command via su -c with absolute stability."""
    try:
        # Wrap command in su -c and execute via subprocess
        full_cmd = f'su -c {shlex.quote(command)}'
        process = subprocess.run(
            full_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60
        )
        return process.returncode, process.stdout.strip(), process.stderr.strip()
    except Exception as e:
        logger.error(f"Shell Error: {e}")
        return -1, "", str(e)

def load_config():
    global admin_ids, device_name, server_link
    try:
        if not CONFIG_PATH.exists():
            default = {"DEVICE_NAME": "DEV_1", "BOT_TOKEN": "", "ADMIN_IDS": [], "SERVER_LINK": ""}
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(default, indent=2))
        
        raw = json.loads(CONFIG_PATH.read_text())
        admin_ids = [int(x) for x in raw.get("ADMIN_IDS", [])]
        device_name = raw.get("DEVICE_NAME", "DEV_1")
        server_link = raw.get("SERVER_LINK", "")
        return raw.get("BOT_TOKEN", "").strip()
    except Exception as e:
        logger.error(f"Config Load Error: {e}")
        return ""

token = load_config()
if not token:
    print("CRITICAL: BOT_TOKEN is missing!")
    sys.exit(1)

bot = telebot.TeleBot(token, parse_mode="HTML")

# ==========================================
# DATABASE INJECTION & SCREENSHOT
# ==========================================

def inject_cookie_root(cookie_value: str) -> tuple[bool, str]:
    """Strict Root Injection using absolute Termux sqlite3 path."""
    try:
        # 1. Stop Roblox
        run_root("am force-stop com.roblox.client")
        time.sleep(1)

        # 2. Prepare SQL
        # Mandatory sequence: DELETE ALL -> INSERT
        insert_sql = (
            "INSERT INTO cookies (creation_utc, host_key, name, value, path, expires_utc, is_secure, is_httponly, last_access_utc, has_expires, is_persistent, priority) "
            f"VALUES (strftime('%s','now')*1000000, '.roblox.com', '.ROBLOSECURITY', '{cookie_value}', '/', (strftime('%s','now')+31536000)*1000000, 1, 1, strftime('%s','now')*1000000, 1, 1, 1);"
        )
        
        # Absolute path used for all DB operations
        sql_cmd = f"{SQLITE_BIN} {DB_PATH} \"DELETE FROM cookies; {insert_sql} REINDEX cookies; VACUUM;\""
        
        code, out, err = run_root(sql_cmd)
        if code != 0:
            return False, f"SQL Error: {err or out}"

        # 3. Permissions fix
        run_root(f"chmod 600 {DB_PATH}")
        return True, "Cookie Injected"
    except Exception as e:
        return False, f"Injection Crash: {e}"

def take_screenshot() -> str | None:
    """Take screenshot using system binary and return path."""
    try:
        # su -c "/system/bin/screencap -p /sdcard/screen.png"
        code, _, err = run_root("/system/bin/screencap -p " + SCREENSHOT_PATH)
        if code == 0:
            return SCREENSHOT_PATH
        logger.error(f"Screencap failed: {err}")
        return None
    except Exception as e:
        logger.error(f"Screenshot Exception: {e}")
        return None

# ==========================================
# MONITORING & STABILITY
# ==========================================

def get_system_stats():
    """RAM calculation with failure fallback."""
    try:
        code, out, _ = run_root("cat /proc/meminfo")
        if code != 0: return "RAM: Normal", "0"
        
        total_kb = 0
        free_kb = 0
        for line in out.splitlines():
            if line.startswith("MemTotal:"): total_kb = int(line.split()[1])
            if line.startswith("MemAvailable:"): free_kb = int(line.split()[1])
            elif line.startswith("MemFree:") and free_kb == 0: free_kb = int(line.split()[1])
        
        # Glitch Fallback
        if total_kb > 64 * 1024 * 1024 or total_kb <= 0:
            ram_txt = "RAM: Normal"
        else:
            used_p = ((total_kb - free_kb) / total_kb) * 100
            ram_txt = f"RAM: {used_p:.1f}% ({total_kb//1024}MB)"
        
        # Connections
        code_c, out_c, _ = run_root("cat /proc/net/tcp | grep ' 01 ' | wc -l")
        con = out_c.strip() if code_c == 0 else "0"
        
        return ram_txt, con
    except:
        return "RAM: Normal", "0"

# ==========================================
# UI & HANDLERS (TELEBOT)
# ==========================================

def main_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🚀 START FARM", callback_data="farm:start"),
        types.InlineKeyboardButton("🛑 STOP FARM", callback_data="farm:stop")
    )
    kb.add(
        types.InlineKeyboardButton("📸 Screenshot", callback_data="sys:screenshot"),
        types.InlineKeyboardButton("🔄 Reboot", callback_data="sys:reboot")
    )
    kb.add(types.InlineKeyboardButton("⚙️ Settings", callback_data="sys:settings"))
    return kb

@bot.message_handler(commands=['start', 'menu'])
def cmd_start(message):
    if message.from_user.id not in admin_ids: return
    ram, con = get_system_stats()
    text = (
        f"<b>AEGIS MONOLITH v3.0 (TeleBot)</b>\n"
        f"───────────────────\n"
        f"DEVICE: <code>{device_name}</code>\n"
        f"{ram}\n"
        f"CONNECTIONS: {con}\n"
        f"SILENT MODE: {'ON' if silent_mode else 'OFF'}\n"
    )
    bot.send_message(message.chat.id, text, reply_markup=main_keyboard())

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    global silent_mode, server_link
    if call.from_user.id not in admin_ids: return

    try:
        if call.data == "farm:start":
            bot.answer_callback_query(call.id, "Starting Injection...")
            # Note: Injection needs cookie, but for simple start we just launch
            link = server_link or "https://www.roblox.com/games/start?placeId=123"
            cmd = f"am start -a android.intent.action.VIEW -d '{link}' com.roblox.client"
            run_root(cmd)
            bot.send_message(call.message.chat.id, "✅ Farm Started")
            
        elif call.data == "farm:stop":
            bot.answer_callback_query(call.id, "Stopping Roblox...")
            run_root("am force-stop com.roblox.client")
            bot.send_message(call.message.chat.id, "🛑 Roblox Stopped")
            
        elif call.data == "sys:screenshot":
            bot.answer_callback_query(call.id, "Capturing Screen...")
            path = take_screenshot()
            if path and os.path.exists(path):
                with open(path, 'rb') as photo:
                    bot.send_photo(call.message.chat.id, photo, caption=f"📸 Screenshot: {device_name}")
                run_root("rm " + path)
            else:
                bot.send_message(call.message.chat.id, "❌ Screenshot Failed")
                
        elif call.data == "sys:reboot":
            bot.answer_callback_query(call.id, "Rebooting...")
            run_root("reboot")
            
        elif call.data == "sys:settings":
            silent_mode = not silent_mode
            bot.answer_callback_query(call.id, f"Silent: {'ON' if silent_mode else 'OFF'}")
            # Refresh
            cmd_start(call.message)

    except Exception as e:
        logger.error(f"Callback Error: {e}")
        bot.answer_callback_query(call.id, "Error")

@bot.message_handler(func=lambda m: m.text and len(m.text) > 200)
def handle_cookie(message):
    if message.from_user.id not in admin_ids: return
    
    bot.send_message(message.chat.id, "⏳ <b>Injecting Cookie via Root Shell...</b>")
    ok, res = inject_cookie_root(message.text.strip())
    
    if ok:
        bot.send_message(message.chat.id, "✅ Cookie Injected. Starting Farm...")
        link = server_link or "https://www.roblox.com/games/start?placeId=123"
        run_root(f"am start -a android.intent.action.VIEW -d '{link}' com.roblox.client")
    else:
        bot.send_message(message.chat.id, f"❌ Injection Failed: {res}")

# ==========================================
# MAIN LOOP
# ==========================================

def main():
    print("Project MONOLITH v3.0 (TeleBot Edition) starting...")
    
    # Permission Fix
    run_root("chown -R $(id -u):$(id -g) .")
    run_root("rm -f .bot.pid")
    
    # Log Init
    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, "a").close()
    run_root(f"chmod 777 {LOG_FILE}")
    logger.add(LOG_FILE, rotation="10 MB")

    logger.info("Monolith v3.0 Online (TeleBot)")

    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=40)
        except Exception as e:
            logger.error(f"Polling Error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
