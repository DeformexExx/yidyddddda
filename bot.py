import os

import asyncio
import html
import json
import shlex
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import telebot
from telebot import util
from loguru import logger

from core.bash_utils import run_bash
from core.monitor import SystemMonitor
from core.ui_manager import build_dashboard, build_main_text, get_device_page, settings_menu_kb

PROJECT_ROOT = "/data/data/com.termux/files/home/aegis_watchdog"
CONFIG_PATH = Path(PROJECT_ROOT) / "config.json"


@dataclass
class AppConfig:
    device_name: str
    bot_token: str
    admin_ids: list[int]
    git_repo_url: str


@dataclass
class DeviceState:
    name: str
    pid: int
    output: str = "Idle"
    enabled: bool = True


@dataclass
class SessionState:
    selected_device: str
    page: str = "main"


class Persistence:
    silent_mode: bool = False


persistence = Persistence()


def load_config() -> AppConfig:
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return AppConfig(
        device_name=str(raw.get("DEVICE_NAME", "DEV_1")),
        bot_token=str(raw.get("BOT_TOKEN", "")).strip(),
        admin_ids=[int(x) for x in raw.get("ADMIN_IDS", [])],
        git_repo_url=str(raw.get("GIT_REPO_URL", "")).strip(),
    )


def discover_devices(default_name: str) -> list[str]:
    names = [p.stem for p in Path(".").glob("DEV_*.json")]
    if default_name not in names:
        names.insert(0, default_name)
    return sorted(set(names))


config = load_config()
bot = telebot.TeleBot(config.bot_token, parse_mode="HTML")
monitor = SystemMonitor()

sessions: dict[int, SessionState] = {}
tracked: dict[int, tuple[int, int]] = {}
devices = {n: DeviceState(name=n, pid=os.getpid()) for n in discover_devices(config.device_name)}


def run_async(coro):
    return asyncio.run(coro)


def is_admin(user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    if not config.admin_ids:
        return True
    return user_id in config.admin_ids


def safe_send(chat_id: int, text: str, reply_markup=None):
    for i, chunk in enumerate(util.smart_split(text, chars_per_string=3500)):
        bot.send_message(chat_id, chunk, reply_markup=reply_markup if i == 0 else None)


def safe_edit(chat_id: int, msg_id: int, text: str, reply_markup=None):
    bot.edit_message_text(text, chat_id, msg_id, reply_markup=reply_markup)


def get_session(user_id: int) -> SessionState:
    if user_id not in sessions:
        sessions[user_id] = SessionState(selected_device=next(iter(devices.keys())))
    return sessions[user_id]


def render_main(user_id: int):
    s = get_session(user_id)
    return build_main_text(config.device_name, s.selected_device, persistence.silent_mode), build_dashboard(None, list(devices.keys()), None, None, None)


def render_device(device_id: str):
    snap = run_async(monitor.snapshot(devices[device_id].pid))
    snap["output"] = devices[device_id].output
    return get_device_page(device_id, snap)


def shell(command: str, root: bool = False, timeout: int = 120):
    return run_async(run_bash(command, root=root, timeout=timeout))


def update_page(chat_id: int, user_id: int):
    if chat_id not in tracked:
        return
    _, msg_id = tracked[chat_id]
    sess = get_session(user_id)
    if sess.page == "device":
        text, kb = render_device(sess.selected_device)
    elif sess.page == "settings":
        text, kb = "<pre>SETTINGS</pre>", settings_menu_kb(persistence.silent_mode)
    else:
        text, kb = render_main(user_id)
    safe_edit(chat_id, msg_id, text, kb)


def refresh_loop():
    while True:
        try:
            for chat_id, (uid, _) in list(tracked.items()):
                if sessions.get(uid) and sessions[uid].page == "device":
                    update_page(chat_id, uid)
            time.sleep(10)
        except Exception:
            time.sleep(10)


def monitor_loop():
    async def _loop():
        while True:
            await monitor.reboot_if_needed()
            restarted = await monitor.watchdog_tick()
            if restarted and not persistence.silent_mode and tracked:
                c = next(iter(tracked.keys()))
                safe_send(c, html.escape("Restarting device..."))
            await asyncio.sleep(10)

    asyncio.run(_loop())


@bot.message_handler(commands=["start", "menu"])
def start(message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    s = get_session(message.from_user.id)
    s.page = "main"
    txt, kb = render_main(message.from_user.id)
    m = bot.send_message(message.chat.id, txt, reply_markup=kb)
    tracked[message.chat.id] = (message.from_user.id, m.message_id)


@bot.message_handler(commands=["exec"])
def exec_cmd(message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    s = get_session(message.from_user.id)
    dev = devices[s.selected_device]
    cmd = message.text.replace("/exec", "", 1).strip()
    if not cmd:
        safe_send(message.chat.id, html.escape("Usage: /exec <command>"))
        return
    code, out, err = shell(cmd, root=True, timeout=60)
    payload = html.escape(out if out else err if err else "(no output)")
    safe_send(message.chat.id, f"<pre>[{html.escape(dev.name)}|PID:{dev.pid}] Exit: {code}\n{payload}</pre>")


def perform_update() -> tuple[int, str, str]:
    os.chdir(PROJECT_ROOT)
    project_dir = shlex.quote(PROJECT_ROOT)
    script_path = Path(PROJECT_ROOT) / "update_nuclear.sh"
    python_bin = shlex.quote(sys.executable)

    script = f"""#!/data/data/com.termux/files/usr/bin/bash
set +e
cd {project_dir} || exit 1
export PATH=/data/data/com.termux/files/usr/bin:/data/data/com.termux/files/usr/bin/applets:/system/bin:/system/xbin
export LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib
export HOME=/data/data/com.termux/files/home
/system/bin/su -c \"pkill -f python || true\"
git -c safe.directory='*' fetch --all && git -c safe.directory='*' reset --hard origin/main && git -c safe.directory='*' clean -fd
/system/bin/su -c \"rm -rf watchdog.log __pycache__\"
/system/bin/su -c \"chown -R \\$(id -u):\\$(id -g) .\"
nohup {python_bin} {shlex.quote(str(Path(PROJECT_ROOT) / 'bot.py'))} >> watchdog.log 2>&1 &
"""
    script_path.write_text(script, encoding="utf-8")
    os.chmod(script_path, 0o755)
    code, out, err = shell(
        f"nohup /data/data/com.termux/files/usr/bin/bash {shlex.quote(str(script_path))} > /dev/null 2>&1 &",
        root=False,
        timeout=10,
    )
    return code, out, err


@bot.message_handler(commands=["update"])
def update_cmd(message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    code, out, err = perform_update()
    if code != 0:
        safe_send(message.chat.id, f"<pre>Update failed\n{html.escape(err or out)}</pre>")
        return
    safe_send(message.chat.id, html.escape("Detached update started; restarting..."))
    os._exit(0)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("dev:"))
def open_device(call):
    dev = call.data.split(":", 1)[1]
    s = get_session(call.from_user.id)
    s.selected_device = dev
    s.page = "device"
    text, kb = render_device(dev)
    safe_edit(call.message.chat.id, call.message.message_id, text, kb)
    tracked[call.message.chat.id] = (call.from_user.id, call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data == "menu:main")
def back_main(call):
    s = get_session(call.from_user.id)
    s.page = "main"
    text, kb = render_main(call.from_user.id)
    safe_edit(call.message.chat.id, call.message.message_id, text, kb)
    tracked[call.message.chat.id] = (call.from_user.id, call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data == "menu:settings")
def settings(call):
    s = get_session(call.from_user.id)
    s.page = "settings"
    safe_edit(call.message.chat.id, call.message.message_id, "<pre>SETTINGS</pre>", settings_menu_kb(persistence.silent_mode))
    tracked[call.message.chat.id] = (call.from_user.id, call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data == "set:silent")
def toggle_silent(call):
    persistence.silent_mode = not persistence.silent_mode
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=settings_menu_kb(persistence.silent_mode))
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data == "set:update")
def update_btn(call):
    bot.answer_callback_query(call.id, "Updating...")
    code, out, err = perform_update()
    if code == 0:
        os._exit(0)
    bot.answer_callback_query(call.id, "Update failed")


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("all:"))
def all_actions(call):
    act = call.data.split(":", 1)[1]
    for d in devices.values():
        d.enabled = (act == "start")
        d.output = "Started" if d.enabled else "Stopped"
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("act:"))
def device_actions(call):
    _, act, dev = call.data.split(":", 2)
    d = devices[dev]
    if act == "start":
        d.enabled = True
        d.output = "Started"
    elif act == "stop":
        d.enabled = False
        d.output = "Stopped"
    elif act == "cookie":
        d.output = "Cookie action"
    elif act == "server":
        d.output = "Server action"
    text, kb = render_device(dev)
    safe_edit(call.message.chat.id, call.message.message_id, text, kb)
    bot.answer_callback_query(call.id)


def main():
    run_async(monitor.set_process_priority(os.getpid()))
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=refresh_loop, daemon=True).start()
    logger.info("Aegis V13 TeleBot started")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=40)
        except Exception:
            time.sleep(3)


if __name__ == "__main__":
    main()
