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
    wait_mode: str | None = None  # None | WAIT_COOKIE | WAIT_SERVER


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
    # Root-level duplicate cleanup to reduce Telegram 409 conflicts, excluding current PID.
    current_pid = os.getpid()
    run_async(
        run_bash(
            f"for p in $(pgrep -f python); do [ \"$p\" != \"{current_pid}\" ] && kill -9 $p; done",
            root=True,
            timeout=8,
        )
    )

    # Additional scoped guard for previous bot instance.
    pid_file = Path(".bot.pid")
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (TypeError, ValueError):
            old_pid = 0

        if old_pid and old_pid != current_pid:
            run_async(run_bash(f"kill -9 {old_pid}", root=True, timeout=5))

    pid_file.write_text(str(current_pid), encoding="utf-8")


def run_shell(command: str, root: bool = False, timeout: int = 120) -> tuple[int, str, str]:
    return run_async(run_bash(command, root=root, timeout=timeout))


def device_package(device_name: str) -> str:
    # Runtime target unified to the real package to avoid invalid clone intents.
    return "com.roblox.client"


def target_process_name() -> str:
    # Process management target explicitly requested.
    return "com.roblox.client"


def clean_cookie_input(raw: str) -> str:
    s = (raw or "").strip()
    warning_prefix = "WARNING: DO NOT SHARE THIS"
    if warning_prefix in s:
        idx = s.find("_|WARNING")
        if idx != -1:
            s = s[idx:]
        else:
            s = s.replace(warning_prefix, "").strip()
    return s


def hard_reset_clone(device_name: str) -> tuple[int, str, str]:
    pkg = device_package(device_name)
    # Force-stop policy target requested as com.roblox.client.
    code, out, err = run_shell(f"am force-stop {target_process_name()}", root=True, timeout=20)
    time.sleep(2)
    code2, out2, err2 = run_shell(
        f"am start -n {shlex.quote(pkg)}/com.roblox.client.startup.ActivitySplash",
        root=True,
        timeout=20,
    )
    if code2 != 0:
        return code2, out2, err2
    return code, out, err


def ensure_sqlite3() -> tuple[bool, str]:
    sqlite_abs = "/data/data/com.termux/files/usr/bin/sqlite3"
    pkg_abs = "/data/data/com.termux/files/usr/bin/pkg"

    code, out, err = run_shell(f"test -x {shlex.quote(sqlite_abs)}", root=False, timeout=10)
    if code == 0:
        return True, sqlite_abs

    code, out, err = run_shell(f"{shlex.quote(pkg_abs)} install -y sqlite", root=False, timeout=180)
    if code == 0:
        return True, sqlite_abs
    return False, (err or out or "sqlite install failed")


def inject_cookie_for_device(device_name: str, cookie_value: str) -> tuple[bool, str]:
    ok, sqlite_bin_or_err = ensure_sqlite3()
    if not ok:
        return False, f"sqlite3 unavailable: {sqlite_bin_or_err}"

    sqlite_bin = sqlite_bin_or_err
    clean_cookie = clean_cookie_input(cookie_value)
    if not clean_cookie:
        return False, "Cookie is empty"

    db_path = "/data/data/com.roblox.client/app_webview/Default/Cookies"
    parent_dir = "/data/data/com.roblox.client/app_webview/Default"
    temp_db = "/sdcard/Cookies"

    stop_code, stop_out, stop_err = run_shell("/system/bin/am force-stop com.roblox.client", root=True, timeout=20)
    logger.info(f"cookie_pre_stop [{device_name}] code={stop_code} out={stop_out} err={stop_err}")
    if stop_code != 0:
        return False, (stop_err or stop_out or "force-stop failed; cookie db is still locked by Roblox")

    code, out, err = run_shell(f"cp {shlex.quote(db_path)} {shlex.quote(temp_db)} && chmod 777 {shlex.quote(temp_db)}", root=True, timeout=20)
    logger.info(f"cookie_copy [{device_name}] code={code} out={out} err={err}")
    if code != 0:
        return False, (err or out or "failed to copy cookies db")

    pragma_cmd = f"{shlex.quote(sqlite_bin)} -separator '|' {shlex.quote(temp_db)} \"PRAGMA table_info(cookies);\""
    code, pragma_out, pragma_err = run_shell(pragma_cmd, root=True, timeout=30)
    if code != 0 or not pragma_out.strip():
        return False, (pragma_err or pragma_out or "PRAGMA table_info failed")

    columns: list[dict[str, str | int]] = []
    for line in pragma_out.splitlines():
        parts = line.split("|", 5)
        if len(parts) < 6:
            continue
        columns.append(
            {
                "name": parts[1],
                "type": parts[2] or "",
                "notnull": 1 if parts[3] == "1" else 0,
                "dflt": parts[4],
            }
        )

    if not columns:
        return False, "cookies schema is empty"

    esc_cookie = clean_cookie.replace("'", "''")
    creation_utc = "((strftime('%s','now') + 11644473600) * 1000000)"
    expires_utc = "((strftime('%s','now') + 11644473600 + 31536000) * 1000000)"
    provided: dict[str, str] = {
        "creation_utc": creation_utc,
        "host_key": "'.roblox.com'",
        "name": "'.ROBLOSECURITY'",
        "value": f"'{esc_cookie}'",
        "encrypted_value": "''",
        "path": "'/'",
        "expires_utc": expires_utc,
        "is_secure": "1",
        "is_httponly": "1",
        "last_access_utc": creation_utc,
        "has_expires": "1",
        "is_persistent": "1",
        "priority": "1",
        "samesite": "-1",
        "source_port": "443",
        "last_update_utc": creation_utc,
        "source_scheme": "2",
        "source_type": "0",
        "has_cross_site_ancestor": "0",
        "top_frame_site_key": "''",
        "is_same_party": "0",
    }

    insert_cols: list[str] = []
    insert_vals: list[str] = []
    for col in columns:
        name = str(col["name"])
        col_type = str(col["type"]).upper()
        notnull = int(col["notnull"])
        dflt = str(col["dflt"] or "")

        if name in provided:
            val = provided[name]
        elif notnull:
            if dflt:
                val = dflt
            elif any(k in col_type for k in ("INT", "REAL", "NUM", "BOOL")):
                val = "0"
            else:
                val = "''"
        else:
            continue

        insert_cols.append(name)
        insert_vals.append(val)

    if not insert_cols:
        return False, "no injectable columns detected"

    insert_sql = f"INSERT INTO cookies ({', '.join(insert_cols)}) VALUES ({', '.join(insert_vals)})"
    sql_blob = (
        "DELETE FROM cookies WHERE name = '.ROBLOSECURITY'; "
        f"{insert_sql}; "
        "REINDEX cookies; VACUUM;"
    )

    code, out, err = run_shell(f"{shlex.quote(sqlite_bin)} {shlex.quote(temp_db)} \"{sql_blob}\"", root=True, timeout=90)
    logger.info(f"cookie_sqlite [{device_name}] code={code} out={out} err={err}")
    if code != 0:
        return False, (err or out or "sqlite dynamic insert failed")

    code, owner_out, owner_err = run_shell(f"stat -c %u:%g {shlex.quote(parent_dir)}", root=True, timeout=15)
    logger.info(f"cookie_owner [{device_name}] code={code} out={owner_out} err={owner_err}")
    owner = owner_out.strip() if code == 0 and owner_out.strip() else "10167:10167"

    code, out, err = run_shell(f"cp {shlex.quote(temp_db)} {shlex.quote(db_path)}", root=True, timeout=20)
    logger.info(f"cookie_replace [{device_name}] code={code} out={out} err={err}")
    if code != 0:
        return False, (err or out or "failed to replace cookies db")

    run_shell(f"chown {shlex.quote(owner)} {shlex.quote(db_path)} && chmod 600 {shlex.quote(db_path)}", root=True, timeout=15)

    time.sleep(2)
    launch_code, launch_out, launch_err = run_shell(
        "/system/bin/am start -n com.roblox.client/.startup.ActivitySplash",
        root=True,
        timeout=20,
    )
    logger.info(f"cookie_post_launch [{device_name}] code={launch_code} out={launch_out} err={launch_err}")
    if launch_code != 0:
        return False, (launch_err or launch_out or "clone launch failed")

    return True, "Cookie injected (dynamic schema) and clone relaunched"


def inject_server_for_device(device_name: str, link: str) -> tuple[bool, str]:
    clean_link = (link or "").strip()
    if not clean_link:
        return False, "Link is empty"

    pkg = device_package(device_name)
    db_path = "/data/data/com.roblox.client/app_webview/Default/Cookies"
    parent_dir = "/data/data/com.roblox.client/app_webview/Default"

    # Final ownership sync right before URL VIEW launch.
    own_code, own_out, own_err = run_shell(f"stat -c %u:%g {shlex.quote(parent_dir)}", root=True, timeout=15)
    owner = own_out.strip() if own_code == 0 and own_out.strip() else "10167:10167"
    run_shell(f"chown {shlex.quote(owner)} {shlex.quote(db_path)} && chmod 600 {shlex.quote(db_path)}", root=True, timeout=15)

    server_link = clean_link.replace("'", "'\"'\"'")
    launch_cmd = (
        f"su -c \"nohup am start -a android.intent.action.VIEW "
        f"-d '{server_link}' {pkg} "
        f"> /dev/null 2>&1 &\""
    )
    code, out, err = run_shell(launch_cmd, root=False, timeout=20)
    if code != 0:
        return False, (err or out or "server open failed")
    return True, "Server link sent to clone"


def render_main(user_id: int) -> tuple[str, object]:
    state = get_session(user_id)
    txt = build_main_text(config.device_name, state.selected_device, silent_mode)
    return txt, build_dashboard(None, list(devices.keys()), None, None, None)


def render_device(device_name: str) -> tuple[str, object]:
    snap = run_async(monitor.snapshot(devices[device_name].pid))
    snap["output"] = "ON" if devices[device_name].output_on else "OFF"
    return get_device_page(device_name, snap)


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
git -c safe.directory='*' fetch --all && git -c safe.directory='*' reset --hard origin/main && git clean -fd
/system/bin/su -c \"rm -rf watchdog.log __pycache__\"
/system/bin/su -c \"chown -R \\$(id -u):\\$(id -g) .\"
nohup {python_bin} {shlex.quote(str(Path(PROJECT_ROOT) / 'main.py'))} >> watchdog.log 2>&1 &
"""
    script_path.write_text(script, encoding="utf-8")
    os.chmod(script_path, 0o755)

    safe_send(chat_id, html.escape("☢️ Detached nuclear update started..."))
    run_shell(f"nohup /data/data/com.termux/files/usr/bin/bash {shlex.quote(str(script_path))} > /dev/null 2>&1 &", root=False, timeout=10)
    os._exit(0)


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


@bot.message_handler(func=lambda m: bool(m.text) and not m.text.startswith("/"))
def handle_wait_input(message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return

    state = get_session(message.from_user.id)
    mode = state.wait_mode
    if mode not in ("WAIT_COOKIE", "WAIT_SERVER"):
        return

    dev = state.selected_device
    payload = message.text.strip()

    if mode == "WAIT_COOKIE":
        ok, msg = inject_cookie_for_device(dev, payload)
        state.wait_mode = None
        safe_send(message.chat.id, html.escape(f"✅ [{dev}] {msg}" if ok else f"❌ [{dev}] {msg}"))
        return

    ok, msg = inject_server_for_device(dev, payload)
    state.wait_mode = None
    safe_send(message.chat.id, html.escape(f"✅ [{dev}] {msg}" if ok else f"❌ [{dev}] {msg}"))


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


@bot.callback_query_handler(func=lambda c: c.data in ("set:silent:toggle", "set:silent"))
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
        state = get_session(call.from_user.id)
        state.wait_mode = "WAIT_COOKIE"
        safe_send(call.message.chat.id, html.escape("Ready for input. Send Cookie now."))
        bot.answer_callback_query(call.id, "WAIT_COOKIE")
    elif action == "server":
        state = get_session(call.from_user.id)
        state.wait_mode = "WAIT_SERVER"
        safe_send(call.message.chat.id, html.escape("Ready for input. Send Link now."))
        bot.answer_callback_query(call.id, "WAIT_SERVER")


def monitor_worker() -> None:
    async def runner():
        while True:
            try:
                await monitor.reboot_if_needed()

                for dev in devices.values():
                    if not dev.output_on:
                        continue
                    con = await monitor.get_connections()
                    if con <= monitor.tcp_zombie_threshold:
                        restarted = await monitor.watchdog_tick()
                        if restarted and (not silent_mode):
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

    session_state_path = Path("session_state.json")
    if session_state_path.exists():
        session_state_path.unlink()

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
