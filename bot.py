import asyncio
import json
import os
import shlex
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, executor, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from core.database import Database
from core.injector import InjectionEngine
from core.monitor import SystemMonitor


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

bot = Bot(token=config.bot_token)
dp = Dispatcher(bot, storage=MemoryStorage())


db = Database(db_path="storage.db", server_json_path="server.json")
injector = InjectionEngine()
monitor = SystemMonitor()

waiting_server_from_user: set[int] = set()
waiting_cookie_from_user: set[int] = set()


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
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton(text="🔄 Refresh Status", callback_data="menu:status"))
    kb.add(InlineKeyboardButton(text="🖥 Server Manager", callback_data="menu:servers"))
    kb.add(InlineKeyboardButton(text="🍪 Cookie Manager", callback_data="menu:cookies"))
    kb.add(InlineKeyboardButton(text="♻️ Update", callback_data="sys:update"))
    return kb


def servers_menu_kb(servers, active_server_name: Optional[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    for s in servers:
        prefix = "✅ " if active_server_name == s.name else ""
        kb.row(
            InlineKeyboardButton(text=f"{prefix}{s.name}", callback_data=f"srv:select:{s.name}"),
            InlineKeyboardButton(text="🗑", callback_data=f"srv:del:{s.name}"),
        )
    kb.add(InlineKeyboardButton(text="⬅ Back", callback_data="menu:main"))
    return kb


def cookies_menu_kb(cookies, active_cookie_name: Optional[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    for c in cookies:
        prefix = "✅ " if active_cookie_name == c.nickname else ""
        kb.row(
            InlineKeyboardButton(text=f"{prefix}{c.nickname}", callback_data=f"ck:select:{c.nickname}"),
            InlineKeyboardButton(text="🗑", callback_data=f"ck:del:{c.nickname}"),
        )
    kb.add(InlineKeyboardButton(text="⬅ Back", callback_data="menu:main"))
    return kb


async def run_shell(command: str, root: bool = False, timeout: int = 120) -> tuple[int, str, str]:
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


async def status_text() -> str:
    snap = await monitor.snapshot()
    active_cookie = await db.get_setting("active_cookie_name", default="None")
    active_server = await db.get_setting("active_server_name", default="None")
    tcp_status = "OK" if snap.watchdog_status == "ACTIVE" else snap.watchdog_status

    dashboard = (
        f"🛡 [{config.device_name}] SYSTEM DASHBOARD\n"
        "--------------------------\n"
        f"📈 RAM: [{progress_bar(snap.ram_percent)}] {snap.ram_percent:.0f}%\n"
        f"🧬 CPU: [{progress_bar(snap.cpu_percent)}] {snap.cpu_percent:.0f}%\n"
        f"🌐 TCP: {snap.tcp_connections} Active (Status: {tcp_status})\n"
        "--------------------------\n"
        f"🍪 Active: {active_cookie or 'None'}\n"
        f"🔗 Server: {active_server or 'None'}"
    )
    return f"```\n{dashboard}\n```"


async def perform_update(message: types.Message) -> None:
    code, out, err = await run_shell("git rev-parse --is-inside-work-tree", root=False, timeout=15)
    if code != 0 or "true" not in out.lower():
        await message.answer("❌ Текущая папка не является Git-репозиторием.")
        return

    if config.git_repo_url:
        await run_shell(f"git remote set-url origin {shlex.quote(config.git_repo_url)}", root=False, timeout=20)

    perms = await asyncio.to_thread(snapshot_core_permissions)
    code, out, err = await run_shell("git pull", root=False, timeout=120)
    await asyncio.to_thread(restore_core_permissions, perms)

    if code != 0:
        await message.answer(f"❌ Ошибка обновления:\n```\n{err or out}\n```", parse_mode="Markdown")
        return

    pull_out = (out or err or "").strip()
    if "Already up to date" in pull_out or "Already up-to-date" in pull_out:
        await message.answer("✅ У вас уже установлена последняя версия.")
        return

    await message.answer("📥 Обновления скачаны. Перезагружаюсь...")
    os.execv(sys.executable, ["python"] + sys.argv)


@dp.message_handler(commands=["start", "menu"])
async def cmd_start(message: types.Message):
    await message.answer(await status_text(), reply_markup=main_menu_kb(), parse_mode="Markdown")


@dp.message_handler(commands=["status"])
async def cmd_status(message: types.Message):
    await message.answer(await status_text(), parse_mode="Markdown")


@dp.message_handler(commands=["exec"])
async def cmd_exec(message: types.Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Access denied")
        return

    command = (message.get_args() or "").strip()
    if not command:
        await message.answer("Usage: /exec <shell_command>")
        return

    code, out, err = await run_shell(command, root=True, timeout=60)
    payload = out if out else err
    if not payload:
        payload = "(no output)"
    if len(payload) > 3500:
        payload = payload[:3500] + "..."
    await message.answer(f"Exit: {code}\n\n```\n{payload}\n```", parse_mode="Markdown")


@dp.message_handler(commands=["update"])
async def cmd_update(message: types.Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer("🔄 Проверяю обновления на GitHub...")
    await perform_update(message)


@dp.message_handler(commands=["add_server"])
async def cmd_add_server(message: types.Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Access denied")
        return
    waiting_server_from_user.add(message.from_user.id)
    await message.answer("Send in one line: <name>|<private_server_link>")


@dp.message_handler(commands=["add_cookie"])
async def cmd_add_cookie(message: types.Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Access denied")
        return
    waiting_cookie_from_user.add(message.from_user.id)
    await message.answer("Send in one line: <nickname>|<ROBLOSECURITY_cookie>")


@dp.message_handler(content_types=types.ContentType.TEXT)
async def handle_plain_input(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    uid = message.from_user.id
    text = message.text or ""

    if uid in waiting_server_from_user:
        waiting_server_from_user.discard(uid)
        if "|" not in text:
            await message.answer("Invalid format. Use: <name>|<private_server_link>")
            return
        name, link = [p.strip() for p in text.split("|", 1)]
        if not name or not link:
            await message.answer("Name and link cannot be empty")
            return
        await db.add_or_replace_server(name, link)
        await message.answer(f"Server saved: {name}")
        return

    if uid in waiting_cookie_from_user:
        waiting_cookie_from_user.discard(uid)
        if "|" not in text:
            await message.answer("Invalid format. Use: <nickname>|<ROBLOSECURITY_cookie>")
            return
        nickname, cookie = [p.strip() for p in text.split("|", 1)]
        if not nickname or not cookie:
            await message.answer("Nickname and cookie cannot be empty")
            return
        await db.add_or_replace_cookie(nickname, cookie)
        await message.answer(f"Cookie saved: {nickname}")


@dp.callback_query_handler(lambda c: c.data == "menu:main")
async def cb_main_menu(call: types.CallbackQuery):
    await call.message.edit_text(await status_text(), reply_markup=main_menu_kb(), parse_mode="Markdown")
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "menu:status")
async def cb_status(call: types.CallbackQuery):
    await call.message.edit_text(await status_text(), reply_markup=main_menu_kb(), parse_mode="Markdown")
    await call.answer("Status refreshed")


@dp.callback_query_handler(lambda c: c.data == "menu:servers")
async def cb_servers(call: types.CallbackQuery):
    servers = await db.list_servers()
    active_name = await db.get_setting("active_server_name", default="")
    text = "🖥 Server Manager\n\nUse /add_server to add new."
    await call.message.edit_text(text, reply_markup=servers_menu_kb(servers, active_name or None))
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "menu:cookies")
async def cb_cookies(call: types.CallbackQuery):
    cookies = await db.list_cookies()
    active_name = await db.get_setting("active_cookie_name", default="")
    text = "🍪 Cookie Manager\n\nUse /add_cookie to add or replace."
    await call.message.edit_text(text, reply_markup=cookies_menu_kb(cookies, active_name or None))
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "sys:update")
async def cb_update(call: types.CallbackQuery):
    if not is_admin(call.from_user.id if call.from_user else None):
        await call.answer("Access denied", show_alert=True)
        return
    await call.answer("Running update...")
    await perform_update(call.message)


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("srv:select:"))
async def cb_srv_select(call: types.CallbackQuery):
    name = call.data.split(":", 2)[2]
    rec = await db.get_server(name)
    if not rec:
        await call.answer("Server not found", show_alert=True)
        return
    await db.set_setting("active_server_name", rec.name)
    await db.set_setting("active_server_link", rec.link)
    await call.answer(f"Selected: {rec.name}")
    await cb_servers(call)


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("srv:del:"))
async def cb_srv_del(call: types.CallbackQuery):
    name = call.data.split(":", 2)[2]
    ok = await db.delete_server(name)
    await call.answer("Deleted" if ok else "Not found")
    await cb_servers(call)


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("ck:select:"))
async def cb_ck_select(call: types.CallbackQuery):
    nickname = call.data.split(":", 2)[2]
    rec = await db.get_cookie(nickname)
    if not rec:
        await call.answer("Cookie not found", show_alert=True)
        return

    await db.set_setting("active_cookie_name", rec.nickname)
    result = await injector.inject_cookie(rec.cookie)
    if result.success:
        await call.answer("Cookie injected")
    else:
        await call.answer("Injection failed", show_alert=True)
        await call.message.answer(f"Injection error: {result.message}")

    await cb_cookies(call)


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("ck:del:"))
async def cb_ck_del(call: types.CallbackQuery):
    nickname = call.data.split(":", 2)[2]
    ok = await db.delete_cookie(nickname)
    await call.answer("Deleted" if ok else "Not found")
    await cb_cookies(call)


async def on_startup(_):
    await db.initialize()
    asyncio.create_task(monitor.ram_guard_loop(interval_sec=10))
    asyncio.create_task(monitor.watchdog_loop(interval_sec=15))
    logger.info("Aegis V13 started")


def main() -> None:
    loop = asyncio.get_event_loop()
    loop.run_until_complete(monitor.set_process_priority(os.getpid()))

    logger.add(
        "watchdog.log",
        rotation="10 MB",
        retention=3,
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )

    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)


if __name__ == "__main__":
    main()
