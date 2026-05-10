import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import telebot
from telebot import types
from loguru import logger

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
PROJECT_ROOT = "/data/data/com.termux/files/home/aegis_watchdog"
CONFIG_PATH = Path(PROJECT_ROOT) / "config.json"
SERVER_FILE = Path(PROJECT_ROOT) / "server.txt"
DB_PATH = "/data/data/com.roblox.client/app_webview/Default/Cookies"
SQLITE_BIN = "/data/data/com.termux/files/usr/bin/sqlite3"
LOG_FILE = "watchdog.log"
SDCARD_SCREEN = "/sdcard/screen.png"

# Global State
start_time = time.time()
admin_ids = []
device_name = "DEV_1"
server_link = ""
silent_mode = False

# ==========================================
# CORE UTILS & SHELL (ROOT ONLY)
# ==========================================

def run_root(command: str) -> tuple[int, str, str]:
    """Execute command via /system/bin/su -c with absolute stability."""
    try:
        full_cmd = f'/system/bin/su -c {shlex.quote(command)}'
        process = subprocess.run(
            full_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=120
        )
        return process.returncode, process.stdout.strip(), process.stderr.strip()
    except Exception as e:
        logger.error(f"Root Shell Error: {e}")
        return -1, "", str(e)

def load_config():
    global admin_ids, device_name, server_link
    try:
        # Load Main Config
        if not CONFIG_PATH.exists():
            default = {"DEVICE_NAME": "DEV_1", "BOT_TOKEN": "", "ADMIN_IDS": []}
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(default, indent=2))
        
        raw = json.loads(CONFIG_PATH.read_text())
        admin_ids = [int(x) for x in raw.get("ADMIN_IDS", [])]
        device_name = raw.get("DEVICE_NAME", "DEV_1")
        
        # Load Server Link (Persistence)
        if SERVER_FILE.exists():
            server_link = SERVER_FILE.read_text(encoding="utf-8").strip()
        else:
            server_link = "https://www.roblox.com/games/start?placeId=123"
            SERVER_FILE.write_text(server_link, encoding="utf-8")
            
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
# AUTO-PERMISSION FIX (ON START)
# ==========================================

def startup_optimization():
    """Bulletproof startup routine for permissions."""
    logger.info("Initializing Aegis Prime v4.0 Stabilization...")
    # Permission fix
    run_root(f"chown -R $(id -u):$(id -g) {PROJECT_ROOT}")
    run_root(f"chmod -R 777 {PROJECT_ROOT}")
    # Storage grant
    run_root("pm grant com.termux android.permission.WRITE_EXTERNAL_STORAGE")
    # Cookie DB access
    run_root(f"chmod 777 {DB_PATH}")
    # Cleanup
    run_root(f"rm -f {PROJECT_ROOT}/.bot.pid")

# ==========================================
# STEALTH INJECTION & SYSTEM
# ==========================================

def inject_cookie_root(cookie_value: str) -> tuple[bool, str]:
    """Stealth Cookie Injection via Root."""
    try:
        run_root("am force-stop com.roblox.client")
        time.sleep(1)

        # SQL Sequence with strftime for 1 year ahead
        insert_sql = (
            "INSERT INTO cookies (creation_utc, host_key, name, value, path, expires_utc, is_secure, is_httponly, last_access_utc, has_expires, is_persistent, priority) "
            f"VALUES (strftime('%s','now')*1000000, '.roblox.com', '.ROBLOSECURITY', '{cookie_value}', '/', (strftime('%s','now')+31536000)*1000000, 1, 1, strftime('%s','now')*1000000, 1, 1, 1);"
        )
        
        # Sequence: DELETE -> INSERT
        sql_cmd = f"{SQLITE_BIN} {DB_PATH} \"DELETE FROM cookies; {insert_sql} REINDEX cookies; VACUUM;\""
        
        code, out, err = run_root(sql_cmd)
        if code != 0: return False, f"SQL Error: {err or out}"

        run_root(f"chmod 600 {DB_PATH}")
        # Launch intent
        run_root(f"am start -a android.intent.action.VIEW -d '{server_link}' com.roblox.client")
        
        return True, "Stealth Injection Successful"
    except Exception as e:
        return False, str(e)

# ==========================================
# ULTIMATE VISUALS (DASHBOARD)
# ==========================================

def _bar(percent: float, length: int = 10) -> str:
    p = max(0.0, min(100.0, percent))
    fill = int((p / 100.0) * length)
    return "█" * fill + "░" * (length - fill)

def get_dashboard_text():
    # RAM calculation
    code, out, _ = run_root("cat /proc/meminfo")
    total_kb, free_kb = 1, 0
    if code == 0:
        for line in out.splitlines():
            if line.startswith("MemTotal:"): total_kb = int(line.split()[1])
            if line.startswith("MemAvailable:"): free_kb = int(line.split()[1])

    if total_kb > 64*1024*1024: total_kb = 4*1024*1024 # Glitch Fix
    used_p = ((total_kb - free_kb) / total_kb) * 100
    
    # Root status
    uid_code, uid_out, _ = run_root("id -u")
    root_status = f"ACTIVE (UID {uid_out if uid_code == 0 else '?'})"

    text = (
        "<b>[ AEGIS PRIME v4.0 ]</b>\n"
        "────────────────────\n"
        f"<code>RAM:    [{_bar(used_p)}] {used_p:.1f}%</code>\n"
        f"<code>ROOT:   {root_status}</code>\n"
        f"<code>SERVER: {html_escape(server_link)}</code>\n"
        "────────────────────\n"
    )
    return text

def html_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# ==========================================
# UI & COMMAND HANDLERS
# ==========================================

def main_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🚀 START FARM", callback_data="farm:start"),
        types.InlineKeyboardButton("🛑 STOP FARM", callback_data="farm:stop")
    )
    kb.add(
        types.InlineKeyboardButton("🌐 Сменить сервер", callback_data="sys:change_server"),
        types.InlineKeyboardButton("📸 SCREENSHOT", callback_data="sys:screen")
    )
    kb.add(
        types.InlineKeyboardButton("🔄 Обновить", callback_data="sys:update"),
        types.InlineKeyboardButton("📊 REFRESH", callback_data="sys:refresh")
    )
    return kb

@bot.message_handler(commands=['start', 'status'])
def cmd_start(message):
    if message.from_user.id not in admin_ids: return
    bot.send_message(message.chat.id, get_dashboard_text(), reply_markup=main_keyboard())

@bot.message_handler(commands=['exec'])
def cmd_exec(message):
    if message.from_user.id not in admin_ids: return
    cmd = message.text.replace("/exec", "", 1).strip()
    if not cmd: return
    
    code, out, err = run_root(cmd)
    res = (out + "\n" + err).strip()
    if not res: res = "(no output)"
    
    # Split if output is too long for Telegram
    for chunk in util.smart_split(res, 3000):
        bot.send_message(message.chat.id, f"<b>EXEC RESULT [Code {code}]:</b>\n<code>{html_escape(chunk)}</code>")

def process_server_link(message):
    global server_link
    link = message.text.strip()
    if link.startswith("http"):
        server_link = link
        SERVER_FILE.write_text(server_link, encoding="utf-8")
        bot.send_message(message.chat.id, "✅ Link updated and saved for this session.")
        cmd_start(message)
    else:
        bot.reply_to(message, "❌ Invalid link.")

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    global admin_ids, server_link
    if call.from_user.id not in admin_ids: return

    try:
        if call.data == "farm:start":
            bot.answer_callback_query(call.id, "Launching Farm...")
            run_root(f"am start -a android.intent.action.VIEW -d '{server_link}' com.roblox.client")
            bot.send_message(call.message.chat.id, "🚀 <b>Farm Intent Dispatched.</b>")
            
        elif call.data == "farm:stop":
            bot.answer_callback_query(call.id, "Stopping...")
            run_root("am force-stop com.roblox.client")
            bot.send_message(call.message.chat.id, "🛑 <b>Process Terminated.</b>")
            
        elif call.data == "sys:change_server":
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id, "🌐 Send new Server Link:")
            bot.register_next_step_handler(msg, process_server_link)
            
        elif call.data == "sys:screen":
            bot.answer_callback_query(call.id, "Capturing...")
            run_root(f"/system/bin/screencap -p {SDCARD_SCREEN}")
            local_p = os.path.join(PROJECT_ROOT, "screen.png")
            run_root(f"cp {SDCARD_SCREEN} {local_p}")
            if os.path.exists(local_p):
                with open(local_p, 'rb') as f:
                    bot.send_photo(call.message.chat.id, f, caption="📸 <b>Sight Recieved.</b>")
                os.remove(local_p)
            else:
                bot.send_message(call.message.chat.id, "❌ Screenshot Failed.")
                
        elif call.data == "sys:refresh":
            bot.edit_message_text(get_dashboard_text(), call.message.chat.id, call.message.message_id, reply_markup=main_keyboard())
            bot.answer_callback_query(call.id, "Refreshed")
            
        elif call.data == "sys:update":
            bot.answer_callback_query(call.id, "Updating...")
            run_root(f"git config --global --add safe.directory {PROJECT_ROOT}")
            run_root("git fetch --all && git reset --hard origin/main")
            bot.send_message(call.message.chat.id, "✅ <b>Update complete. Restarting...</b>")
            # Bulletproof Restart
            os.execv(sys.executable, ['python'] + sys.argv)

    except Exception as e:
        logger.error(f"Callback Error: {e}")

@bot.message_handler(func=lambda m: m.text and len(m.text) > 200)
def handle_cookie(message):
    if message.from_user.id not in admin_ids: return
    bot.send_message(message.chat.id, "⏳ <b>Initiating Stealth Injection...</b>")
    ok, res = inject_cookie_root(message.text.strip())
    bot.send_message(message.chat.id, f"{'✅' if ok else '❌'} <b>{res}</b>")

# ==========================================
# MAIN ENTRY
# ==========================================

def main():
    print("Project MONOLITH [AEGIS PRIME v4.0] starting...")
    startup_optimization()
    
    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, "a").close()
    run_root(f"chmod 777 {LOG_FILE}")
    logger.add(LOG_FILE, rotation="10 MB", retention=3)
    
    logger.info("Aegis Prime v4.0 Online (TeleBot Edition)")

    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=40)
        except Exception as e:
            logger.error(f"Polling Crash: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
