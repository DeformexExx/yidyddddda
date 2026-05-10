import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# AIOGRAM IMPORTS
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.utils import executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
PROJECT_ROOT = "/data/data/com.termux/files/home/aegis_watchdog"
CONFIG_PATH = Path(PROJECT_ROOT) / "config.json"
DB_PATH = "/data/data/com.roblox.client/app_webview/Default/Cookies"
SQLITE_BIN = "/data/data/com.termux/files/usr/bin/sqlite3"
LOG_FILE = "watchdog.log"

# Global State
silent_mode = False
admin_ids = []
device_name = "DEV_1"

# ==========================================
# CORE UTILS & SHELL (ROOT ONLY)
# ==========================================

def run_root(command: str) -> tuple[int, str, str]:
    """Execute command via su -c bypass."""
    try:
        full_cmd = f'su -c "{command}"'
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

def load_config():
    global admin_ids, device_name
    try:
        if not CONFIG_PATH.exists():
            default = {"DEVICE_NAME": "DEV_1", "BOT_TOKEN": "", "ADMIN_IDS": []}
            CONFIG_PATH.write_text(json.dumps(default, indent=2))
        
        raw = json.loads(CONFIG_PATH.read_text())
        admin_ids = [int(x) for x in raw.get("ADMIN_IDS", [])]
        device_name = raw.get("DEVICE_NAME", "DEV_1")
        return raw.get("BOT_TOKEN", "").strip()
    except Exception as e:
        print(f"Config Error: {e}")
        return ""

# ==========================================
# 2. MANDATORY INJECTION LOGIC (ROOT SHELL)
# ==========================================

def inject_cookie_root(cookie_value: str) -> tuple[bool, str]:
    """Absolute Root Injection via Termux Sqlite3 binary."""
    try:
        # Stop Roblox
        run_root("am force-stop com.roblox.client")
        time.sleep(1)

        # SQL Preparation
        # Using strftime('%s','now') for fresh timestamps as ordered
        insert_sql = f"""
        INSERT INTO cookies (creation_utc, host_key, name, value, path, expires_utc, is_secure, is_httponly, last_access_utc, has_expires, is_persistent, priority) 
        VALUES (strftime('%s','now')*1000000, '.roblox.com', '.ROBLOSECURITY', '{cookie_value}', '/', (strftime('%s','now')+31536000)*1000000, 1, 1, strftime('%s','now')*1000000, 1, 1, 1);
        """
        
        # Mandatory Sequence: DELETE ALL -> INSERT
        # Using the absolute path to Termux sqlite3 binary
        sql_command = f"{SQLITE_BIN} {DB_PATH} 'DELETE FROM cookies; {insert_sql} REINDEX cookies; VACUUM;'"
        
        code, out, err = run_root(sql_command)
        if code != 0:
            return False, f"SQL Error: {err or out}"

        # Fix permission lock
        run_root(f"chmod 600 {DB_PATH}")
        
        # Intent start
        run_root("am start -n com.roblox.client/com.roblox.client.startup.ActivitySplash")
        
        return True, "Injection Successful (Root Only)"
    except Exception as e:
        return False, f"System Error: {e}"

# ==========================================
# 4. MONITORING & RAM FIX
# ==========================================

def get_system_stats():
    """RAM Display Glitch Fix and Connections."""
    try:
        # Get RAM via /proc/meminfo
        code, out, _ = run_root("cat /proc/meminfo")
        total_kb = 0
        free_kb = 0
        for line in out.splitlines():
            if line.startswith("MemTotal:"): total_kb = int(line.split()[1])
            if line.startswith("MemFree:"): free_kb = int(line.split()[1])
        
        # GLITCH FALLBACK (>64GB)
        display_total = "4.0 GB" if total_kb > 64 * 1024 * 1024 else f"{total_kb / 1024 / 1024:.1f} GB"
        display_free = f"{free_kb / 1024 / 1024:.1f} GB"
        
        # Connections
        code_c, out_c, _ = run_root("cat /proc/net/tcp | grep ' 01 ' | wc -l")
        con = out_c.strip() if code_c == 0 else "0"
        
        return display_total, display_free, con
    except:
        return "4.0 GB", "0.0 GB", "0"

# ==========================================
# 3. BOT INTERFACE (AIOGRAM)
# ==========================================

token = load_config()
if not token:
    print("CRITICAL: No BOT_TOKEN found in config.json")
    sys.exit(1)

bot = Bot(token=token, parse_mode="HTML")
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

def get_main_kb():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🚀 START FARM", callback_data="btn_start"),
        InlineKeyboardButton("🔄 REBOOT SYSTEM", callback_data="btn_reboot"),
        InlineKeyboardButton("⚙️ SETTINGS", callback_data="btn_settings")
    )
    return kb

@dp.message_handler(commands=['start', 'menu'])
async def cmd_start(message: types.Message):
    global silent_mode
    if message.from_user.id not in admin_ids: return
    
    total, free, con = get_system_stats()
    text = (
        f"<b>AEGIS MONOLITH v2.0</b>\n"
        f"───────────────────\n"
        f"DEVICE: <code>{device_name}</code>\n"
        f"RAM: {total} (FREE: {free})\n"
        f"ACTIVE CON: {con}\n"
        f"SILENT: {'ON' if silent_mode else 'OFF'}\n"
    )
    await message.answer(text, reply_markup=get_main_kb())

@dp.callback_query_handler(lambda c: c.data == 'btn_start')
async def process_start(callback_query: types.CallbackQuery):
    global silent_mode
    if callback_query.from_user.id not in admin_ids: return
    
    await bot.answer_callback_query(callback_query.id, "Initiating Farm Start...")
    
    # 1. Stop Roblox
    run_root("am force-stop com.roblox.client")
    
    # 2. Restart Splash
    run_root("am start -n com.roblox.client/com.roblox.client.startup.ActivitySplash")
    
    await bot.send_message(callback_query.from_user.id, "✅ Farm process started via Intent")

@dp.callback_query_handler(lambda c: c.data == 'btn_reboot')
async def process_reboot(callback_query: types.CallbackQuery):
    if callback_query.from_user.id not in admin_ids: return
    await bot.answer_callback_query(callback_query.id, "Rebooting...")
    run_root("reboot")

@dp.callback_query_handler(lambda c: c.data == 'btn_settings')
async def process_settings(callback_query: types.CallbackQuery):
    global silent_mode
    if callback_query.from_user.id not in admin_ids: return
    
    silent_mode = not silent_mode
    await bot.answer_callback_query(callback_query.id, f"Silent: {'ON' if silent_mode else 'OFF'}")
    
    total, free, con = get_system_stats()
    text = (
        f"<b>AEGIS MONOLITH v2.0</b>\n"
        f"───────────────────\n"
        f"DEVICE: {device_name}\n"
        f"RAM: {total} (FREE: {free})\n"
        f"ACTIVE CON: {con}\n"
        f"SILENT: {'ON' if silent_mode else 'OFF'}\n"
    )
    await bot.edit_message_text(text, callback_query.from_user.id, callback_query.message.message_id, reply_markup=get_main_kb())

@dp.message_handler(lambda m: m.text and len(m.text) > 100)
async def handle_cookie_input(message: types.Message):
    """Handle raw cookie input for injection."""
    if message.from_user.id not in admin_ids: return
    
    await message.answer("⏳ <b>MONOLITH INJECTION IN PROGRESS...</b>")
    
    ok, res = inject_cookie_root(message.text.strip())
    
    if ok:
        await message.answer(f"✅ <b>SUCCESS:</b> {res}")
    else:
        await message.answer(f"❌ <b>FAILED:</b> {res}")

# ==========================================
# 5. STARTUP & DEPLOYMENT
# ==========================================

async def on_startup(dp):
    print("Deploying Aegis Monolith v2.0...")
    
    # Permission Stabilization
    run_root("chown -R $(id -u):$(id -g) .")
    run_root("rm -f .bot.pid")
    
    # Log Init
    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, "a").close()
    run_root(f"chmod 777 {LOG_FILE}")
    
    print("System Online. Polling Started.")

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    executor.start_polling(dp, on_startup=on_startup)
