import asyncio
import json
import os
import shlex
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger

from core.database import CookieRecord, Database, ServerRecord
from core.injector import InjectionEngine
from core.monitor import SystemMonitor


CONFIG_PATH = Path("config.json")


@dataclass(slots=True)
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


def progress_bar(percent: float, length: int = 20) -> str:
    pct = max(0.0, min(100.0, percent))
    fill = int((pct / 100.0) * length)
    return "█" * fill + "░" * (length - fill)


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Refresh Status", callback_data="menu:status")],
            [InlineKeyboardButton(text="🖥 Server Manager", callback_data="menu:servers")],
            [InlineKeyboardButton(text="🍪 Cookie Manager", callback_data="menu:cookies")],
            [InlineKeyboardButton(text="♻️ Update", callback_data="sys:update")],
        ]
    )


def servers_menu_kb(servers: list[ServerRecord], active_server_name: Optional[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for s in servers:
        prefix = "✅ " if active_server_name == s.name else ""
        rows.append([
            InlineKeyboardButton(text=f"{prefix}{s.name}", callback_data=f"srv:select:{s.name}"),
            InlineKeyboardButton(text="🗑", callback_data=f"srv:del:{s.name}"),
        ])
    rows.append([InlineKeyboardButton(text="⬅ Back", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cookies_menu_kb(cookies: list[CookieRecord], active_cookie_name: Optional[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for c in cookies:
        prefix = "✅ " if active_cookie_name == c.nickname else ""
        rows.append([
            InlineKeyboardButton(text=f"{prefix}{c.nickname}", callback_data=f"ck:select:{c.nickname}"),
            InlineKeyboardButton(text="🗑", callback_data=f"ck:del:{c.nickname}"),
        ])
    rows.append([InlineKeyboardButton(text="⬅ Back", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class WatchdogBot:
    def __init__(self, config: AppConfig) -> None:
        if not config.bot_token:
            raise RuntimeError("BOT_TOKEN is empty in config.json")

        self.config = config
        self.bot = Bot(token=config.bot_token)
        self.dp = Dispatcher()

        self.db = Database(db_path="storage.db", server_json_path="server.json")
        self.injector = InjectionEngine()
        self.monitor = SystemMonitor()

        self.waiting_server_from_user: set[int] = set()
        self.waiting_cookie_from_user: set[int] = set()

        self._register_handlers()

    def _is_admin(self, user_id: Optional[int]) -> bool:
        if user_id is None:
            return False
        if not self.config.admin_ids:
            return True
        return user_id in self.config.admin_ids

    async def run_shell(self, command: str, *, root: bool = False, timeout: int = 60) -> tuple[int, str, str]:
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

    async def status_text(self) -> str:
        snap = await self.monitor.snapshot()
        active_cookie = await self.db.get_setting("active_cookie_name", default="None")
        active_server = await self.db.get_setting("active_server_name", default="None")
        tcp_status = "OK" if snap.watchdog_status == "ACTIVE" else snap.watchdog_status

        dashboard = (
            f"🛡 [{self.config.device_name}] SYSTEM DASHBOARD\n"
            f"--------------------------\n"
            f"📈 RAM: [{progress_bar(snap.ram_percent)}] {snap.ram_percent:.0f}%\n"
            f"🧬 CPU: [{progress_bar(snap.cpu_percent)}] {snap.cpu_percent:.0f}%\n"
            f"🌐 TCP: {snap.tcp_connections} Active (Status: {tcp_status})\n"
            f"--------------------------\n"
            f"🍪 Active: {active_cookie or 'None'}\n"
            f"🔗 Server: {active_server or 'None'}"
        )
        return f"```\n{dashboard}\n```"

    async def start_background_tasks(self) -> None:
        await self.db.initialize()
        asyncio.create_task(self.monitor.ram_guard_loop(interval_sec=10), name="ram_guard")
        asyncio.create_task(self.monitor.watchdog_loop(interval_sec=15), name="watchdog")

    def _register_handlers(self) -> None:
        self.dp.message.register(self.cmd_start, CommandStart())
        self.dp.message.register(self.cmd_menu, Command("menu"))
        self.dp.message.register(self.cmd_status, Command("status"))
        self.dp.message.register(self.cmd_exec, Command("exec"))
        self.dp.message.register(self.cmd_update, Command("update"))
        self.dp.message.register(self.cmd_add_server, Command("add_server"))
        self.dp.message.register(self.cmd_add_cookie, Command("add_cookie"))

        self.dp.callback_query.register(self.cb_main_menu, F.data == "menu:main")
        self.dp.callback_query.register(self.cb_status, F.data == "menu:status")
        self.dp.callback_query.register(self.cb_servers, F.data == "menu:servers")
        self.dp.callback_query.register(self.cb_cookies, F.data == "menu:cookies")
        self.dp.callback_query.register(self.cb_update, F.data == "sys:update")

        self.dp.callback_query.register(self.cb_srv_select, F.data.startswith("srv:select:"))
        self.dp.callback_query.register(self.cb_srv_del, F.data.startswith("srv:del:"))
        self.dp.callback_query.register(self.cb_ck_select, F.data.startswith("ck:select:"))
        self.dp.callback_query.register(self.cb_ck_del, F.data.startswith("ck:del:"))

        self.dp.message.register(self.handle_plain_input)

    async def cmd_start(self, message: Message) -> None:
        await message.answer(await self.status_text(), reply_markup=main_menu_kb(), parse_mode="Markdown")

    async def cmd_menu(self, message: Message) -> None:
        await message.answer(await self.status_text(), reply_markup=main_menu_kb(), parse_mode="Markdown")

    async def cmd_status(self, message: Message) -> None:
        await message.answer(await self.status_text(), parse_mode="Markdown")

    async def cmd_exec(self, message: Message) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            await message.answer("Access denied")
            return
        if not message.text:
            return

        command = message.text.replace("/exec", "", 1).strip()
        if not command:
            await message.answer("Usage: /exec <shell_command>")
            return

        code, out, err = await self.run_shell(command, root=True, timeout=60)
        payload = out if out else err
        if not payload:
            payload = "(no output)"
        if len(payload) > 3500:
            payload = payload[:3500] + "..."
        await message.answer(f"Exit: {code}\n\n```\n{payload}\n```", parse_mode="Markdown")

    async def cmd_update(self, message: Message) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            await message.answer("Access denied")
            return
        await message.answer("🔄 Проверяю обновления на GitHub...")
        await self.perform_update(message)

    async def cmd_add_server(self, message: Message) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            await message.answer("Access denied")
            return
        self.waiting_server_from_user.add(message.from_user.id)
        await message.answer("Send in one line: <name>|<private_server_link>")

    async def cmd_add_cookie(self, message: Message) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            await message.answer("Access denied")
            return
        self.waiting_cookie_from_user.add(message.from_user.id)
        await message.answer("Send in one line: <nickname>|<ROBLOSECURITY_cookie>")

    async def handle_plain_input(self, message: Message) -> None:
        if not message.from_user or not message.text:
            return

        uid = message.from_user.id
        if not self._is_admin(uid):
            return

        if uid in self.waiting_server_from_user:
            self.waiting_server_from_user.discard(uid)
            if "|" not in message.text:
                await message.answer("Invalid format. Use: <name>|<private_server_link>")
                return
            name, link = [p.strip() for p in message.text.split("|", 1)]
            if not name or not link:
                await message.answer("Name and link cannot be empty")
                return
            await self.db.add_or_replace_server(name, link)
            await message.answer(f"Server saved: {name}")
            return

        if uid in self.waiting_cookie_from_user:
            self.waiting_cookie_from_user.discard(uid)
            if "|" not in message.text:
                await message.answer("Invalid format. Use: <nickname>|<ROBLOSECURITY_cookie>")
                return
            nickname, cookie = [p.strip() for p in message.text.split("|", 1)]
            if not nickname or not cookie:
                await message.answer("Nickname and cookie cannot be empty")
                return
            await self.db.add_or_replace_cookie(nickname, cookie)
            await message.answer(f"Cookie saved: {nickname}")
            return

    async def cb_main_menu(self, call: CallbackQuery) -> None:
        if call.message:
            await call.message.edit_text(await self.status_text(), reply_markup=main_menu_kb(), parse_mode="Markdown")
        await call.answer()

    async def cb_status(self, call: CallbackQuery) -> None:
        if call.message:
            await call.message.edit_text(await self.status_text(), reply_markup=main_menu_kb(), parse_mode="Markdown")
        await call.answer("Status refreshed")

    async def cb_servers(self, call: CallbackQuery) -> None:
        servers = await self.db.list_servers()
        active_name = await self.db.get_setting("active_server_name", default="")
        text = "🖥 Server Manager\n\nUse /add_server to add new."
        if call.message:
            await call.message.edit_text(text, reply_markup=servers_menu_kb(servers, active_name or None))
        await call.answer()

    async def cb_cookies(self, call: CallbackQuery) -> None:
        cookies = await self.db.list_cookies()
        active_name = await self.db.get_setting("active_cookie_name", default="")
        text = "🍪 Cookie Manager\n\nUse /add_cookie to add or replace."
        if call.message:
            await call.message.edit_text(text, reply_markup=cookies_menu_kb(cookies, active_name or None))
        await call.answer()

    async def cb_update(self, call: CallbackQuery) -> None:
        if not call.from_user or not self._is_admin(call.from_user.id):
            await call.answer("Access denied", show_alert=True)
            return
        await call.answer("Running update...")
        if call.message:
            await self.perform_update(call.message)

    async def cb_srv_select(self, call: CallbackQuery) -> None:
        name = call.data.split(":", 2)[2] if call.data else ""
        rec = await self.db.get_server(name)
        if not rec:
            await call.answer("Server not found", show_alert=True)
            return
        await self.db.set_setting("active_server_name", rec.name)
        await self.db.set_setting("active_server_link", rec.link)
        await call.answer(f"Selected: {rec.name}")
        await self.cb_servers(call)

    async def cb_srv_del(self, call: CallbackQuery) -> None:
        name = call.data.split(":", 2)[2] if call.data else ""
        ok = await self.db.delete_server(name)
        await call.answer("Deleted" if ok else "Not found")
        await self.cb_servers(call)

    async def cb_ck_select(self, call: CallbackQuery) -> None:
        nickname = call.data.split(":", 2)[2] if call.data else ""
        rec = await self.db.get_cookie(nickname)
        if not rec:
            await call.answer("Cookie not found", show_alert=True)
            return

        await self.db.set_setting("active_cookie_name", rec.nickname)
        result = await self.injector.inject_cookie(rec.cookie)
        if result.success:
            await call.answer("Cookie injected")
        else:
            await call.answer("Injection failed", show_alert=True)
            if call.message:
                await call.message.answer(f"Injection error: {result.message}")

        await self.cb_cookies(call)

    async def cb_ck_del(self, call: CallbackQuery) -> None:
        nickname = call.data.split(":", 2)[2] if call.data else ""
        ok = await self.db.delete_cookie(nickname)
        await call.answer("Deleted" if ok else "Not found")
        await self.cb_cookies(call)

    def _snapshot_core_permissions(self) -> dict[str, tuple[int, int, int]]:
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

    def _restore_core_permissions(self, snapshot: dict[str, tuple[int, int, int]]) -> None:
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

    async def perform_update(self, message: Message) -> None:
        code, out, err = await self.run_shell("git rev-parse --is-inside-work-tree", root=False, timeout=15)
        if code != 0 or "true" not in out.lower():
            await message.answer("❌ Текущая папка не является Git-репозиторием.")
            return

        if self.config.git_repo_url:
            await self.run_shell(f"git remote set-url origin {shlex.quote(self.config.git_repo_url)}", root=False, timeout=20)

        perms = await asyncio.to_thread(self._snapshot_core_permissions)
        code, out, err = await self.run_shell("git pull", root=False, timeout=120)
        await asyncio.to_thread(self._restore_core_permissions, perms)

        if code != 0:
            await message.answer(f"❌ Ошибка обновления:\n```\n{err or out}\n```", parse_mode="Markdown")
            return

        pull_out = (out or err or "").strip()
        if "Already up to date" in pull_out or "Already up-to-date" in pull_out:
            await message.answer("✅ У вас уже установлена последняя версия.")
            return

        await message.answer("📥 Обновления скачаны. Перезагружаюсь...")
        os.execv(sys.executable, ["python"] + sys.argv)

    async def run(self) -> None:
        logger.info("Starting Aegis V13 Watchdog bot")
        await self.start_background_tasks()
        await self.dp.start_polling(self.bot)


async def main() -> None:
    # Hardening must run first.
    bootstrap_monitor = SystemMonitor()
    await bootstrap_monitor.set_process_priority(os.getpid())

    logger.add(
        "watchdog.log",
        rotation="10 MB",
        retention=3,
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )

    config = load_config()
    app = WatchdogBot(config)
    await app.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.warning("Bot stopped")
