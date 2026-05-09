import asyncio
import json
import os
import shlex
import stat
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import telebot
from loguru import logger
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from core.database import Database
from core.injector import InjectionEngine
from core.monitor import SystemMonitor
from core.ui_manager import build_dashboard

CONFIG_PATH = Path("config.json")


@dataclass
class AppConfig:
    device_name: str
    bot_token: str
    admin_ids: list[int]
    git_repo_url: str


DEFAULT_CONFIG = {
    "DEVICE_NAME": "DEV1",
    "BOT_TOKEN": "",
    "ADMIN_IDS": [],
    "GIT_REPO_URL": "",
}


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")

    raw = json.loads(path.read_text(encoding="utf-8"))
    merged = {**DEFAULT_CONFIG, **raw}

    admin_ids: list[int] = []
    for item in merged.get("ADMIN_IDS", []):
        try:
            admin_ids.append(int(item))
        except (TypeError, ValueError):
            continue

    return AppConfig(
        device_name=str(merged.get("DEVICE_NAME", "DEV1")),
        bot_token=str(merged.get("BOT_TOKEN", "")).strip(),
        admin_ids=admin_ids,
        git_repo_url=str(merged.get("GIT_REPO_URL", "")).strip(),
    )


config = load_config()
if not config.bot_token:
    raise RuntimeError("BOT_TOKEN is empty in config.json")

bot = telebot.TeleBot(config.bot_token, parse_mode="Markdown")

db = Database(db_path="storage.db", server_json_path="server.json")
injector = InjectionEngine()
monitor = SystemMonitor()

waiting_server_from_user: set[int] = set()
waiting_cookie_from_user: set[int] = set()


def run_async(coro):
    return asyncio.run(coro)


def is_admin(user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    if not config.admin_ids:
        return True
    return user_id in config.admin_ids


def progress_bar(percent: float, length: int = 20) -> str:
    pct = max(0.0, min(100.0, percent))
    fill = int((pct / 100.0) * length)
    return "█" * fill + "░" * (length - fill)


def main_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("🔄 Refresh Status", callback_data="menu:status"))
    kb.row(InlineKeyboardButton("🖥 Server Manager", callback_data="menu:servers"))
    kb.row(InlineKeyboardButton("🍪 Cookie Manager", callback_data="menu:cookies"))
    kb.row(InlineKeyboardButton("♻️ Update", callback_data="sys:update"))
    return kb


def servers_menu_kb(servers, active_server_name: Optional[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    for s in servers:
        prefix = "✅ " if active_server_name == s.name else ""
        kb.row(
            InlineKeyboardButton(f"{prefix}{s.name}", callback_data=f"srv:select:{s.name}"),
            InlineKeyboardButton("🗑", callback_data=f"srv:del:{s.name}"),
        )
    kb.row(InlineKeyboardButton("⬅ Back", callback_data="menu:main"))
    return kb


def cookies_menu_kb(cookies, active_cookie_name: Optional[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    for c in cookies:
        prefix = "✅ " if active_cookie_name == c.nickname else ""
        kb.row(
            InlineKeyboardButton(f"{prefix}{c.nickname}", callback_data=f"ck:select:{c.nickname}"),
            InlineKeyboardButton("🗑", callback_data=f"ck:del:{c.nickname}"),
        )
    kb.row(InlineKeyboardButton("⬅ Back", callback_data="menu:main"))
    return kb


def status_text() -> str:
    snap = run_async(monitor.snapshot(os.getpid()))
    active_cookie = run_async(db.get_setting("active_cookie_name", default="None"))
    active_server = run_async(db.get_setting("active_server_name", default="None"))
    return build_dashboard(config.device_name, snap, active_cookie or "None", active_server or "None")


def run_shell(command: str, root: bool = False, timeout: int = 120) -> tuple[int, str, str]:
    return run_async(_run_shell_async(command, root=root, timeout=timeout))


async def _run_shell_async(command: str, root: bool = False, timeout: int = 120) -> tuple[int, str, str]:
    final_command = f"su -c {shlex.quote(command)}" if root else command
    proc = await asyncio.create_subprocess_shell(
        final_command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, "", f"Timeout after {timeout}s"

    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="ignore").strip(),
        stderr.decode("utf-8", errors="ignore").strip(),
    )


def snapshot_core_permissions() -> dict[str, tuple[int, int, int]]:
    snapshot: dict[str, tuple[int, int, int]] = {}
    core = Path("core")
    if not core.exists():
        return snapshot
    for p in core.rglob("*"):
        try:
            st = p.stat()
            snapshot[str(p)] = (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode))
        except FileNotFoundError:
            continue
    return snapshot


def restore_core_permissions(snapshot: dict[str, tuple[int, int, int]]) -> None:
    for p_str, (uid, gid, mode) in snapshot.items():
        p = Path(p_str)
        if not p.exists():
            continue
        try:
            os.chmod(p, mode)
        except OSError:
            pass
        try:
            os.chown(p, uid, gid)
        except OSError:
            pass


def perform_update(chat_id: int) -> None:
    code, out, err = run_shell("git rev-parse --is-inside-work-tree", root=False, timeout=15)
    if code != 0 or "true" not in out.lower():
        bot.send_message(chat_id, "❌ Текущая папка не является Git-репозиторием.")
        return

    if config.git_repo_url:
        run_shell(f"git remote set-url origin {shlex.quote(config.git_repo_url)}", root=False, timeout=20)

    perms = snapshot_core_permissions()
    code, out, err = run_shell("git pull", root=False, timeout=120)
    restore_core_permissions(perms)

    if code != 0:
        bot.send_message(chat_id, f"❌ Ошибка обновления:\n```\n{err or out}\n```")
        return

    pull_out = (out or err or "").strip()
    if "Already up to date" in pull_out or "Already up-to-date" in pull_out:
        bot.send_message(chat_id, "✅ У вас уже установлена последняя версия.")
        return

    bot.send_message(chat_id, "📥 Обновления скачаны. Перезагружаюсь...")
    os.execv(sys.executable, ["python"] + sys.argv)


@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    bot.send_message(message.chat.id, status_text(), reply_markup=main_menu_kb())


@bot.message_handler(commands=["status"])
def cmd_status(message):
    bot.send_message(message.chat.id, status_text())


@bot.message_handler(commands=["exec"])
def cmd_exec(message):
    if not is_admin(message.from_user.id if message.from_user else None):
        bot.send_message(message.chat.id, "Access denied")
        return

    command = message.text.replace("/exec", "", 1).strip()
    if not command:
        bot.send_message(message.chat.id, "Usage: /exec <shell_command>")
        return

    code, out, err = run_shell(command, root=True, timeout=60)
    payload = out if out else err
    if not payload:
        payload = "(no output)"
    if len(payload) > 3500:
        payload = payload[:3500] + "..."
    bot.send_message(message.chat.id, f"Exit: {code}\n\n```\n{payload}\n```")


@bot.message_handler(commands=["update"])
def cmd_update(message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    bot.send_message(message.chat.id, "🔄 Проверяю обновления на GitHub...")
    perform_update(message.chat.id)


@bot.message_handler(commands=["add_server"])
def cmd_add_server(message):
    if not is_admin(message.from_user.id if message.from_user else None):
        bot.send_message(message.chat.id, "Access denied")
        return
    waiting_server_from_user.add(message.from_user.id)
    bot.send_message(message.chat.id, "Send in one line: <name>|<private_server_link>")


@bot.message_handler(commands=["add_cookie"])
def cmd_add_cookie(message):
    if not is_admin(message.from_user.id if message.from_user else None):
        bot.send_message(message.chat.id, "Access denied")
        return
    waiting_cookie_from_user.add(message.from_user.id)
    bot.send_message(message.chat.id, "Send in one line: <nickname>|<ROBLOSECURITY_cookie>")


@bot.callback_query_handler(func=lambda c: c.data == "menu:main")
def cb_main_menu(call):
    bot.edit_message_text(
        status_text(),
        call.message.chat.id,
        call.message.message_id,
        reply_markup=main_menu_kb(),
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data == "menu:status")
def cb_status(call):
    bot.edit_message_text(
        status_text(),
        call.message.chat.id,
        call.message.message_id,
        reply_markup=main_menu_kb(),
    )
    bot.answer_callback_query(call.id, "Status refreshed")


@bot.callback_query_handler(func=lambda c: c.data == "menu:servers")
def cb_servers(call):
    servers = run_async(db.list_servers())
    active_name = run_async(db.get_setting("active_server_name", default=""))
    text = "🖥 Server Manager\n\nUse /add_server to add new."
    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=servers_menu_kb(servers, active_name or None),
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data == "menu:cookies")
def cb_cookies(call):
    cookies = run_async(db.list_cookies())
    active_name = run_async(db.get_setting("active_cookie_name", default=""))
    text = "🍪 Cookie Manager\n\nUse /add_cookie to add or replace."
    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=cookies_menu_kb(cookies, active_name or None),
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data == "sys:update")
def cb_update(call):
    if not is_admin(call.from_user.id if call.from_user else None):
        bot.answer_callback_query(call.id, "Access denied")
        return
    bot.answer_callback_query(call.id, "Running update...")
    perform_update(call.message.chat.id)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("srv:select:"))
def cb_srv_select(call):
    name = call.data.split(":", 2)[2]
    rec = run_async(db.get_server(name))
    if not rec:
        bot.answer_callback_query(call.id, "Server not found")
        return
    run_async(db.set_setting("active_server_name", rec.name))
    run_async(db.set_setting("active_server_link", rec.link))
    bot.answer_callback_query(call.id, f"Selected: {rec.name}")
    cb_servers(call)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("srv:del:"))
def cb_srv_del(call):
    name = call.data.split(":", 2)[2]
    ok = run_async(db.delete_server(name))
    bot.answer_callback_query(call.id, "Deleted" if ok else "Not found")
    cb_servers(call)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("ck:select:"))
def cb_ck_select(call):
    nickname = call.data.split(":", 2)[2]
    rec = run_async(db.get_cookie(nickname))
    if not rec:
        bot.answer_callback_query(call.id, "Cookie not found")
        return

    run_async(db.set_setting("active_cookie_name", rec.nickname))
    result = run_async(injector.inject_cookie(rec.cookie))
    if result.success:
        bot.answer_callback_query(call.id, "Cookie injected")
    else:
        bot.answer_callback_query(call.id, "Injection failed")
        bot.send_message(call.message.chat.id, f"Injection error: {result.message}")

    cb_cookies(call)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("ck:del:"))
def cb_ck_del(call):
    nickname = call.data.split(":", 2)[2]
    ok = run_async(db.delete_cookie(nickname))
    bot.answer_callback_query(call.id, "Deleted" if ok else "Not found")
    cb_cookies(call)


@bot.message_handler(func=lambda message: True, content_types=["text"])
def handle_plain_input(message):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    uid = message.from_user.id
    text = message.text or ""

    if uid in waiting_server_from_user:
        waiting_server_from_user.discard(uid)
        if "|" not in text:
            bot.send_message(message.chat.id, "Invalid format. Use: <name>|<private_server_link>")
            return
        name, link = [p.strip() for p in text.split("|", 1)]
        if not name or not link:
            bot.send_message(message.chat.id, "Name and link cannot be empty")
            return
        run_async(db.add_or_replace_server(name, link))
        bot.send_message(message.chat.id, f"Server saved: {name}")
        return

    if uid in waiting_cookie_from_user:
        waiting_cookie_from_user.discard(uid)
        if "|" not in text:
            bot.send_message(message.chat.id, "Invalid format. Use: <nickname>|<ROBLOSECURITY_cookie>")
            return
        nickname, cookie = [p.strip() for p in text.split("|", 1)]
        if not nickname or not cookie:
            bot.send_message(message.chat.id, "Nickname and cookie cannot be empty")
            return
        run_async(db.add_or_replace_cookie(nickname, cookie))
        bot.send_message(message.chat.id, f"Cookie saved: {nickname}")


def monitor_worker() -> None:
    async def runner():
        await db.initialize()
        asyncio.create_task(monitor.ram_guard_loop(interval_sec=10))
        asyncio.create_task(monitor.watchdog_loop(interval_sec=15))
        while True:
            await asyncio.sleep(3600)

    asyncio.run(runner())


def main() -> None:
    run_async(monitor.set_process_priority(os.getpid()))

    logger.add(
        "watchdog.log",
        rotation="10 MB",
        retention=3,
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )

    t = threading.Thread(target=monitor_worker, daemon=True)
    t.start()

    logger.info("Aegis V13 started (pyTelegramBotAPI mode)")

    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=40)
        except Exception as exc:
            logger.exception(f"Polling crashed: {exc}")
            time.sleep(3)


if __name__ == "__main__":
    main()
