import asyncio
import html
import json
import os
import shlex
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import telebot
from loguru import logger
from telebot import util

from core.bash_utils import run_bash
from core.database import Database
from core.injector import InjectionEngine
from core.monitor import HealthSnapshot, SystemMonitor
from core.ui_manager import build_dashboard, build_device_text, build_main_text, device_menu_kb, settings_menu_kb

CONFIG_PATH = Path("config.json")


@dataclass
class AppConfig:
    device_name: str
    bot_token: str
    admin_ids: list[int]
    git_repo_url: str


DEFAULT_CONFIG = {
    "DEVICE_NAME": "DEV_1",
    "BOT_TOKEN": "",
    "ADMIN_IDS": [],
    "GIT_REPO_URL": "",
}


@dataclass
class DeviceState:
    name: str
    pid: int
    output_on: bool = True


@dataclass
class SessionState:
    selected_device: str
    view: str = "main"
    message_id: int | None = None


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
        device_name=str(merged.get("DEVICE_NAME", "DEV_1")),
        bot_token=str(merged.get("BOT_TOKEN", "")).strip(),
        admin_ids=admin_ids,
        git_repo_url=str(merged.get("GIT_REPO_URL", "")).strip(),
    )


def discover_devices(default_name: str) -> list[str]:
    names: list[str] = []
    for p in Path(".").glob("DEV_*.json"):
        names.append(p.stem)
    if default_name not in names:
        names.insert(0, default_name)
    return sorted(set(names))


config = load_config()
if not config.bot_token:
    raise RuntimeError("BOT_TOKEN is empty in config.json")

bot = telebot.TeleBot(config.bot_token, parse_mode="HTML")

monitor = SystemMonitor()
db = Database(db_path="storage.db", server_json_path="server.json")
injector = InjectionEngine()

silent_mode = False
sessions: dict[int, SessionState] = {}
tracked_messages: dict[int, tuple[int, int]] = {}  # chat_id -> (user_id, message_id)
devices: dict[str, DeviceState] = {name: DeviceState(name=name, pid=os.getpid()) for name in discover_devices(config.device_name)}


def run_async(coro):
    return asyncio.run(coro)


def is_admin(user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    if not config.admin_ids:
        return True
    return user_id in config.admin_ids


def safe_send(chat_id: int, text: str, reply_markup=None) -> None:
    for chunk in util.smart_split(text, chars_per_string=3500):
        bot.send_message(chat_id, chunk, reply_markup=reply_markup)
        reply_markup = None


def safe_edit(chat_id: int, message_id: int, text: str, reply_markup=None) -> None:
    safe_text = text if "<pre>" in text else html.escape(text)
    if len(safe_text) > 3500:
        safe_text = safe_text[:3500]
    bot.edit_message_text(safe_text, chat_id, message_id, reply_markup=reply_markup)


def get_session(user_id: int) -> SessionState:
    if user_id not in sessions:
        first_device = next(iter(devices.keys()))
        sessions[user_id] = SessionState(selected_device=first_device)
    return sessions[user_id]


def startup_conflict_prevention() -> None:
    # Required bootstrap guard against 409 Conflict
    run_async(run_bash("pkill -f python", root=True, timeout=5))


def run_shell(command: str, root: bool = False, timeout: int = 120) -> tuple[int, str, str]:
    return run_async(run_bash(command, root=root, timeout=timeout))


def render_main(user_id: int) -> tuple[str, object]:
    state = get_session(user_id)
    txt = build_main_text(config.device_name, state.selected_device, silent_mode)
    return txt, build_dashboard(list(devices.keys()))


def render_device(device_name: str) -> tuple[str, object]:
    snap: HealthSnapshot = run_async(monitor.snapshot(devices[device_name].pid))
    txt = build_device_text(device_name, snap, devices[device_name].output_on)
    return txt, device_menu_kb(device_name)


def update_status_message(chat_id: int, user_id: int) -> None:
    state = get_session(user_id)
    tracked = tracked_messages.get(chat_id)
    if not tracked:
        return
    _, message_id = tracked

    try:
        if state.view == "device":
            txt, kb = render_device(state.selected_device)
        elif state.view == "settings":
            txt = "<pre>[ SETTINGS MATRIX ]</pre>"
            kb = settings_menu_kb(silent_mode)
        else:
            txt, kb = render_main(user_id)
        safe_edit(chat_id, message_id, txt, reply_markup=kb)
    except Exception as exc:
        logger.debug(f"auto-refresh edit skip: {exc}")


def auto_refresh_worker() -> None:
    while True:
        try:
            for chat_id, (user_id, _) in list(tracked_messages.items()):
                state = sessions.get(user_id)
                if state and state.view == "device":
                    update_status_message(chat_id, user_id)
            time.sleep(10)
        except Exception as exc:
            logger.exception(f"auto_refresh_worker error: {exc}")
            time.sleep(10)


def perform_update(chat_id: int) -> None:
    code, out, err = run_shell("git rev-parse --is-inside-work-tree", root=False, timeout=15)
    if code != 0 or "true" not in out.lower():
        safe_send(chat_id, html.escape("❌ Not a git repository"))
        return

    if config.git_repo_url:
        run_shell(f"git remote set-url origin {shlex.quote(config.git_repo_url)}", root=False, timeout=20)

    code, out, err = run_shell("git pull", root=False, timeout=120)
    if code != 0:
        safe_send(chat_id, f"<pre>❌ Update failed\n{html.escape(err or out)}</pre>")
        return

    if "Already up to date" in out or "Already up-to-date" in out:
        safe_send(chat_id, html.escape("✅ Already up to date"))
        return

    safe_send(chat_id, html.escape("📥 System updated, restarting..."))
    os.execv(sys.executable, ["python"] + sys.argv)


@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    state = get_session(message.from_user.id)
    state.view = "main"
    text, kb = render_main(message.from_user.id)
    msg = bot.send_message(message.chat.id, text, reply_markup=kb)
    tracked_messages[message.chat.id] = (message.from_user.id, msg.message_id)
    state.message_id = msg.message_id


@bot.message_handler(commands=["status"])
def cmd_status(message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    state = get_session(message.from_user.id)
    state.view = "device"
    text, kb = render_device(state.selected_device)
    msg = bot.send_message(message.chat.id, text, reply_markup=kb)
    tracked_messages[message.chat.id] = (message.from_user.id, msg.message_id)
    state.message_id = msg.message_id


@bot.message_handler(commands=["exec"])
def cmd_exec(message):
    if not is_admin(message.from_user.id if message.from_user else None):
        safe_send(message.chat.id, html.escape("Access denied"))
        return

    state = get_session(message.from_user.id)
    dev = state.selected_device
    pid = devices[dev].pid

    command = message.text.replace("/exec", "", 1).strip()
    if not command:
        safe_send(message.chat.id, html.escape("Usage: /exec <shell_command>"))
        return

    code, out, err = run_shell(command, root=True, timeout=60)
    payload = out if out else err
    if not payload:
        payload = "(no output)"
    safe_payload = html.escape(payload)
    safe_send(message.chat.id, f"<pre>[{html.escape(dev)}|PID:{pid}] Exit: {code}\n\n{safe_payload}</pre>")


@bot.message_handler(commands=["update"])
def cmd_update(message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    safe_send(message.chat.id, html.escape("🔄 Updating..."))
    perform_update(message.chat.id)


@bot.callback_query_handler(func=lambda c: c.data == "menu:main")
def cb_main(call):
    if not is_admin(call.from_user.id if call.from_user else None):
        return
    state = get_session(call.from_user.id)
    state.view = "main"
    text, kb = render_main(call.from_user.id)
    safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=kb)
    tracked_messages[call.message.chat.id] = (call.from_user.id, call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("dev:"))
def cb_device(call):
    dev = call.data.split(":", 1)[1]
    if dev not in devices:
        bot.answer_callback_query(call.id, "Unknown device")
        return
    state = get_session(call.from_user.id)
    state.selected_device = dev
    state.view = "device"
    text, kb = render_device(dev)
    safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=kb)
    tracked_messages[call.message.chat.id] = (call.from_user.id, call.message.message_id)
    bot.answer_callback_query(call.id, f"Selected {dev}")


@bot.callback_query_handler(func=lambda c: c.data == "menu:settings")
def cb_settings(call):
    state = get_session(call.from_user.id)
    state.view = "settings"
    safe_edit(call.message.chat.id, call.message.message_id, "<pre>[ SETTINGS MATRIX ]</pre>", reply_markup=settings_menu_kb(silent_mode))
    tracked_messages[call.message.chat.id] = (call.from_user.id, call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data == "set:update")
def cb_set_update(call):
    bot.answer_callback_query(call.id, "Updating...")
    perform_update(call.message.chat.id)


@bot.callback_query_handler(func=lambda c: c.data == "set:silent:toggle")
def cb_set_silent(call):
    global silent_mode
    silent_mode = not silent_mode
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=settings_menu_kb(silent_mode))
    bot.answer_callback_query(call.id, f"Silent {'ON' if silent_mode else 'OFF'}")


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("all:"))
def cb_all(call):
    action = call.data.split(":", 1)[1]
    if action == "start":
        for dev in devices.values():
            dev.output_on = True
        bot.answer_callback_query(call.id, "START ALL done")
    elif action == "stop":
        for dev in devices.values():
            dev.output_on = False
        bot.answer_callback_query(call.id, "STOP ALL done")


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("act:"))
def cb_actions(call):
    _, action, dev = call.data.split(":", 2)
    if dev not in devices:
        bot.answer_callback_query(call.id, "Unknown device")
        return

    if action == "start":
        devices[dev].output_on = True
        bot.answer_callback_query(call.id, f"{dev} started")
    elif action == "stop":
        devices[dev].output_on = False
        bot.answer_callback_query(call.id, f"{dev} stopped")
    elif action == "cookie":
        bot.answer_callback_query(call.id, "Cookie slot ready")
    elif action == "server":
        bot.answer_callback_query(call.id, "Server slot ready")


def monitor_worker() -> None:
    async def runner():
        while True:
            try:
                ram = await monitor.get_ram_percent()
                if ram >= monitor.ram_critical_percent:
                    await monitor.reboot_now()

                for dev in devices.values():
                    if not dev.output_on:
                        continue
                    con = await monitor.get_clone_connections()
                    if con <= monitor.tcp_zombie_threshold:
                        await monitor.restart_roblox()
                        if not silent_mode:
                            first_chat = next(iter(tracked_messages.keys()), None)
                            if first_chat:
                                safe_send(first_chat, html.escape(f"⚠️ Restarting device {dev.name} (CON={con})"))
                await asyncio.sleep(10)
            except Exception as exc:
                logger.exception(f"monitor_worker error: {exc}")
                await asyncio.sleep(10)

    asyncio.run(runner())


def main() -> None:
    logger.add("watchdog.log", rotation="10 MB", retention=3, enqueue=True, backtrace=False, diagnose=False)

    startup_conflict_prevention()
    run_async(monitor.set_process_priority(os.getpid()))
    run_async(db.initialize())

    threading.Thread(target=monitor_worker, daemon=True).start()
    threading.Thread(target=auto_refresh_worker, daemon=True).start()

    logger.info("Aegis V13 main.py started")

    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=40)
        except Exception as exc:
            logger.exception(f"Polling crashed: {exc}")
            time.sleep(3)


if __name__ == "__main__":
    main()
