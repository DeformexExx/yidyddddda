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
DB_PATH = "/data/data/com.roblox.client/app_webview/Default/Cookies"
SQLITE_BIN = "/data/data/com.termux/files/usr/bin/sqlite3"
SCREENCAP_BIN = "/system/bin/screencap"
LOG_FILE = "watchdog.log"
SDCARD_SCREEN = "/sdcard/screen.png"
SERVER_FILE = Path(PROJECT_ROOT) / "current_server.txt"

# Global State
start_time = time.time()
silent_mode = False
admin_ids = []
device_name = "DEV_1"
server_link = ""

# ==========================================
# CORE UTILS & SHELL (ROOT ONLY)
# ==========================================

def run_root(command: str) -> tuple[int, str, str]:
    """Execute command via /system/bin/su -c with absolute stability."""
    try:
        # Wrap command in su -c and execute via subprocess
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
            default = {"DEVICE_NAME": "DEV_1", "BOT_TOKEN": "", "ADMIN_IDS": [], "SERVER_LINK": ""}
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(default, indent=2))
        
        raw = json.loads(CONFIG_PATH.read_text())
        admin_ids = [int(x) for x in raw.get("ADMIN_IDS", [])]
        device_name = raw.get("DEVICE_NAME", "DEV_1")
        
        # Load Dynamic Server Link (Persistence)
        if SERVER_FILE.exists():
            server_link = SERVER_FILE.read_text(encoding="utf-8").strip()
        else:
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
# PERMISSIONS & STABILIZATION
# ==========================================

def auto_stabilize():
    """Automatic permission and environment setup."""
    logger.info("Running auto-stabilization...")
    # Grant WES to Termux
    run_root("pm grant com.termux android.permission.WRITE_EXTERNAL_STORAGE")
    # Fix Cookie DB permissions
    run_root(f"chmod 777 {DB_PATH}")
    # Fix local directory ownership
    run_root(f"chown -R $(id -u):$(id -g) {PROJECT_ROOT}")
    # Cleanup PID
    run_root(f"rm -f {PROJECT_ROOT}/.bot.pid")

# ==========================================
# ROBЛОКС INJECTION & SCREENSHOT
# ==========================================

def inject_cookie_root(cookie_value: str) -> tuple[bool, str]:
    """Production-ready Root Injection."""
    try:
        # Stop Roblox
        run_root("am force-stop com.roblox.client")
        time.sleep(1)

        # SQL Injection Sequence
        insert_sql = (
            "INSERT INTO cookies (creation_utc, host_key, name, value, path, expires_utc, is_secure, is_httponly, last_access_utc, has_expires, is_persistent, priority) "
            f"VALUES (strftime('%s','now')*1000000, '.roblox.com', '.ROBLOSECURITY', '{cookie_value}', '/', (strftime('%s','now')+31536000)*1000000, 1, 1, strftime('%s','now')*1000000, 1, 1, 1);"
        )
        
        sql_cmd = f"{SQLITE_BIN} {DB_PATH} \"DELETE FROM cookies; {insert_sql} REINDEX cookies; VACUUM;\""
        
        code, out, err = run_root(sql_cmd)
        if code != 0:
            return False, f"SQL Error: {err or out}"

        # Permission reset
        run_root(f"chmod 600 {DB_PATH}")

        # Launch Intent
        link = server_link or "https://www.roblox.com/games/start?placeId=123"
        run_root(f"am start -a android.intent.action.VIEW -d '{link}' com.roblox.client")
        
        return True, "Cookie Injected & Link Launched"
    except Exception as e:
        return False, f"Injection Crash: {e}"

def capture_screen_root() -> str | None:
    """Execute root screencap and transfer to local directory."""
    try:
        local_path = os.path.join(PROJECT_ROOT, "screen.png")
        # Step 1: Capture to SDCARD
        code, _, err = run_root(f"{SCREENCAP_BIN} -p {SDCARD_SCREEN}")
        if code != 0: return None
        # Step 2: Move/Copy to Termux Dir (due to access restrictions)
        run_root(f"cp {SDCARD_SCREEN} {local_path}")
        run_root(f"chmod 777 {local_path}")
        return local_path
    except:
        return None

# ==========================================
# AESTHETIC DASHBOARD & STATUS
# ==========================================

def get_uptime():
    delta = timedelta(seconds=int(time.time() - start_time))
    h, m = divmod(delta.seconds // 60, 60)
    return f"{delta.days}d {h}h {m}m"

def _bar(percent: float, length: int = 10) -> str:
    p = max(0.0, min(100.0, percent))
    fill = int((p / 100.0) * length)
    return "█" * fill + "░" * (length - fill)

def get_dashboard_text():
    # RAM Metrics
    code, out, _ = run_root("cat /proc/meminfo")
    total_kb = 1
    free_kb = 0
    if code == 0:
        for line in out.splitlines():
            if line.startswith("MemTotal:"): total_kb = int(line.split()[1])
            if line.startswith("MemAvailable:"): free_kb = int(line.split()[1])
    
    # Kernel Glitch Fallback
    if total_kb > 64*1024*1024: total_kb = 4*1024*1024
    used_p = ((total_kb - free_kb) / total_kb) * 100
    
    # Root Status
    root_check, _, _ = run_root("id")
    root_status = "ENABLED (Superuser)" if root_check == 0 else "DISABLED"

    text = (
        "<code>SYSTEM STATUS [v2.0]</code>\n"
        "<code>--------------------</code>\n"
        f"<code>RAM:   [{_bar(used_p)}] {used_p:.1f}% (Free: {free_kb//1024/1024:.1f}GB)</code>\n"
        f"<code>ROOT:  {root_status}</code>\n"
        f"<code>AUTH:  SESSION ACTIVE</code>\n"
        f"<code>UPTIME: {get_uptime()}</code>\n"
        f"<code>DEVICE: {device_name}</code>\n"
    )
    return text

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
        types.InlineKeyboardButton("📸 SCREENSHOT", callback_data="sys:screen"),
        types.InlineKeyboardButton("🌐 Сменить сервер", callback_data="sys:change_server")
    )
    kb.add(
        types.InlineKeyboardButton("🔄 REFRESH", callback_data="sys:refresh"),
        types.InlineKeyboardButton("🛠 EXECUTOR", callback_data="sys:exec_mode")
    )
    return kb

@bot.message_handler(commands=['start', 'status', 'menu'])
def cmd_status(message):
    global admin_ids
    if message.from_user.id not in admin_ids: return
    bot.send_message(message.chat.id, get_dashboard_text(), reply_markup=main_keyboard())

@bot.message_handler(commands=['exec'])
def cmd_exec(message):
    global admin_ids
    if message.from_user.id not in admin_ids: return
    
    cmd = message.text.replace("/exec", "", 1).strip()
    if not cmd:
        bot.reply_to(message, "Usage: <code>/exec <command></code>")
        return

    code, out, err = run_root(cmd)
    payload = out if code == 0 else err
    if not payload: payload = "(No Output)"
    
    bot.send_message(message.chat.id, f"<b>RESULT [Exit {code}]:</b>\n<code>{html_escape(payload)}</code>")

def html_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

@bot.message_handler(commands=['screen'])
def cmd_screen(message):
    global admin_ids
    if message.from_user.id not in admin_ids: return
    
    path = capture_screen_root()
    if path and os.path.exists(path):
        with open(path, 'rb') as f:
            bot.send_photo(message.chat.id, f, caption=f"📸 <b>SYSTEM SIGHT [v2.0]</b>")
        run_root(f"rm {path}")
    else:
        bot.reply_to(message, "❌ <b>Screencap Failed</b>")

@bot.message_handler(commands=['update'])
def cmd_update(message):
    global admin_ids
    if message.from_user.id not in admin_ids: return
    
    bot.send_message(message.chat.id, "🔄 <b>Starting Smart Update...</b>")
    
    # Git Sequence
    run_root(f"git config --global --add safe.directory {PROJECT_ROOT}")
    code, out, err = run_root(f"cd {PROJECT_ROOT} && git fetch --all && git reset --hard origin/main")
    
    if code == 0:
        bot.send_message(message.chat.id, "✅ <b>Git pull successful. Finalizing...</b>")
        # Stabilization
        run_root(f"chown -R $(id -u):$(id -g) {PROJECT_ROOT}")
        run_root(f"chmod +x {os.path.join(PROJECT_ROOT, 'main.py')}")
        
        # Auto-restart via os.execv
        logger.warning("RESTARTING: Smart Update Triggered")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    else:
        bot.send_message(message.chat.id, f"❌ <b>Update Failed:</b>\n<code>{err or out}</code>")

def process_server_link_step(message):
    global server_link
    try:
        link = message.text.strip()
        if not link.startswith("http"):
            bot.reply_to(message, "❌ Invalid link. Must start with http/https.")
            return

        server_link = link
        SERVER_FILE.write_text(server_link, encoding="utf-8")
        bot.send_message(message.chat.id, "✅ Link updated and saved for this session.")
        cmd_status(message)
    except Exception as e:
        bot.reply_to(message, f"❌ Error saving link: {e}")

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    global admin_ids, server_link
    if call.from_user.id not in admin_ids: return

    try:
        if call.data == "farm:start":
            bot.answer_callback_query(call.id, "Launching Farm...")
            # Dynamically use the CURRENT server_link
            link = server_link or "https://www.roblox.com/games/start?placeId=123"
            logger.info(f"Launching farm with link: {link}")
            run_root(f"am start -a android.intent.action.VIEW -d '{link}' com.roblox.client")
            bot.send_message(call.message.chat.id, "✅ <b>FARM INTENT SENT</b>")
            
        elif call.data == "farm:stop":
            bot.answer_callback_query(call.id, "Stopping Roblox...")
            run_root("am force-stop com.roblox.client")
            bot.send_message(call.message.chat.id, "🛑 <b>ROBLOX TERMINATED</b>")
            
        elif call.data == "sys:screen":
            cmd_screen(call.message)
            bot.answer_callback_query(call.id)
            
        elif call.data == "sys:change_server":
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id, "🌐 Please send the new Server Link:")
            bot.register_next_step_handler(msg, process_server_link_step)
            
        elif call.data == "sys:refresh":
            bot.edit_message_text(get_dashboard_text(), call.message.chat.id, call.message.message_id, reply_markup=main_keyboard())
            bot.answer_callback_query(call.id, "Refreshed")
            
        elif call.data == "sys:exec_mode":
            bot.send_message(call.message.chat.id, "⌨️ <b>Executor Ready.</b> Use <code>/exec &lt;cmd&gt;</code>")
            bot.answer_callback_query(call.id)

    except Exception as e:
        logger.error(f"Callback Error: {e}")

@bot.message_handler(func=lambda m: m.text and len(m.text) > 200)
def handle_cookie_auto(message):
    global admin_ids
    if message.from_user.id not in admin_ids: return
    
    bot.send_message(message.chat.id, "⏳ <b>PRO INJECTION START...</b>")
    ok, res = inject_cookie_root(message.text.strip())
    
    if ok:
        bot.send_message(message.chat.id, f"✅ <b>{res}</b>")
    else:
        bot.send_message(message.chat.id, f"❌ <b>{res}</b>")

# ==========================================
# MAIN ENTRY
# ==========================================

def main():
    print("Project MONOLITH [v2.0 PRO] starting...")
    
    # 1. Stabilization
    auto_stabilize()
    
    # 2. Logging
    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, "a").close()
    run_root(f"chmod 777 {LOG_FILE}")
    logger.add(LOG_FILE, rotation="10 MB", retention=3)
    
    logger.info("Aegis Monolith v2.0 Online (TeleBot)")

    # 3. Infinity Polling
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=40)
        except Exception as e:
            logger.error(f"Polling Crash: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
