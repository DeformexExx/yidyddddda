from __future__ import annotations

import html

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from core.monitor import HealthSnapshot


def progress_bar(percent: float, length: int = 18) -> str:
    pct = max(0.0, min(100.0, percent))
    fill = int((pct / 100.0) * length)
    return "█" * fill + "░" * (length - fill)


def status_emoji(status: str) -> str:
    if status == "ACTIVE":
        return "🟢"
    if status == "UNSTABLE":
        return "🟡"
    return "🔴"


def build_dashboard(devices_list: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    for dev in devices_list:
        safe_dev = html.escape(dev)
        kb.row(InlineKeyboardButton(f"🧷 {safe_dev}", callback_data=f"dev:{dev}"))
    kb.row(
        InlineKeyboardButton("🚀 START ALL", callback_data="all:start"),
        InlineKeyboardButton("🛑 STOP ALL", callback_data="all:stop"),
    )
    kb.row(InlineKeyboardButton("⚙️ SETTINGS", callback_data="menu:settings"))
    return kb


def device_menu_kb(device_name: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("▶️ START", callback_data=f"act:start:{device_name}"),
        InlineKeyboardButton("🛑 STOP", callback_data=f"act:stop:{device_name}"),
    )
    kb.row(
        InlineKeyboardButton("🍪 COOKIE", callback_data=f"act:cookie:{device_name}"),
        InlineKeyboardButton("🔗 SERVER", callback_data=f"act:server:{device_name}"),
    )
    kb.row(InlineKeyboardButton("⬅️ BACK", callback_data="menu:main"))
    return kb


def settings_menu_kb(silent_mode: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    silent_label = "🔇 SILENT MODE ON" if silent_mode else "🔔 SILENT MODE OFF"
    kb.row(InlineKeyboardButton("🔄 UPDATE", callback_data="set:update"))
    kb.row(InlineKeyboardButton(silent_label, callback_data="set:silent:toggle"))
    kb.row(InlineKeyboardButton("⬅️ BACK", callback_data="menu:main"))
    return kb


def build_main_text(device_name: str, selected: str, silent_mode: bool) -> str:
    safe_device_name = html.escape(device_name)
    safe_selected = html.escape(selected)
    silent_txt = "ON" if silent_mode else "OFF"
    return (
        "<pre>\n"
        "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        "┃ AEGIS V13 :: DIGITAL UNDERGROUND   ┃\n"
        f"┃ HOST: {safe_device_name:<29}┃\n"
        f"┃ SELECTED: {safe_selected:<25}┃\n"
        f"┃ SILENT MODE: {silent_txt:<22}┃\n"
        "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n"
        "</pre>"
    )


def build_device_text(device_name: str, snap: HealthSnapshot, output_on: bool) -> str:
    emoji = status_emoji(snap.watchdog_status)
    out_state = "ON" if output_on else "OFF"
    safe_name = html.escape(device_name)
    safe_status = html.escape(snap.watchdog_status)
    return (
        "<pre>\n"
        "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃ DEVICE: {safe_name:<28}┃\n"
        "┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫\n"
        f"┃ RAM  [{progress_bar(snap.ram_percent)}] {snap.ram_percent:>5.1f}% ┃\n"
        f"┃ CPU  [{progress_bar(snap.cpu_percent)}] {snap.cpu_percent:>5.1f}% ┃\n"
        f"┃ CON: {snap.tcp_connections:<4} THR: {snap.threads:<5} {emoji} {safe_status:<8}┃\n"
        f"┃ OUTPUT: {out_state:<28}┃\n"
        "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n"
        "</pre>"
    )
