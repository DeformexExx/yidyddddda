from __future__ import annotations

import html
from typing import Any

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup


def _bar(percent: float, length: int = 16) -> str:
    p = max(0.0, min(100.0, percent))
    fill = int((p / 100.0) * length)
    return "█" * fill + "░" * (length - fill)


def _status_emoji(status: str) -> str:
    if status == "ACTIVE":
        return "🟢"
    if status == "UNSTABLE":
        return "🟡"
    return "🔴"


def build_dashboard(self=None, device_name=None, snapshot=None, active_cookie=None, active_server=None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    devices_list = self

    if isinstance(devices_list, (list, tuple, set)):
        for dev in devices_list:
            kb.row(InlineKeyboardButton(f"📱 {html.escape(str(dev))}", callback_data=f"dev:{dev}"))
    else:
        dev = str(device_name or devices_list)
        kb.row(InlineKeyboardButton(f"📱 {html.escape(dev)}", callback_data=f"dev:{dev}"))

    kb.row(
        InlineKeyboardButton("🚀 START ALL", callback_data="all:start"),
        InlineKeyboardButton("🛑 STOP ALL", callback_data="all:stop"),
    )
    kb.row(InlineKeyboardButton("⚙️ SETTINGS", callback_data="menu:settings"))
    return kb


def build_main_text(host_name: str, selected_device: str, silent_mode: bool) -> str:
    return (
        "<pre>\n"
        "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        "┃ AEGIS V13 // HUB            ┃\n"
        f"┃ HOST: {html.escape(host_name):<22}┃\n"
        f"┃ FOCUS: {html.escape(selected_device):<21}┃\n"
        f"┃ SILENT: {('ON' if silent_mode else 'OFF'):<20}┃\n"
        "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n"
        "</pre>"
    )


def device_menu_kb(device_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("▶️ START", callback_data=f"act:start:{device_id}"),
        InlineKeyboardButton("🛑 STOP", callback_data=f"act:stop:{device_id}"),
    )
    kb.row(
        InlineKeyboardButton("🍪 COOKIE", callback_data=f"act:cookie:{device_id}"),
        InlineKeyboardButton("🔗 SERVER", callback_data=f"act:server:{device_id}"),
    )
    kb.row(InlineKeyboardButton("⬅️ BACK", callback_data="menu:main"))
    return kb


def settings_menu_kb(silent_mode: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("🔄 UPDATE", callback_data="set:update"))
    kb.row(InlineKeyboardButton(f"🔇 SILENT MODE: {'ON' if silent_mode else 'OFF'}", callback_data="set:silent"))
    kb.row(InlineKeyboardButton("⬅️ BACK", callback_data="menu:main"))
    return kb


def get_device_page(device_id: str, stats: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    status = str(stats.get("status", "UNSTABLE"))
    text = (
        "<pre>\n"
        f"DEVICE: {html.escape(device_id)}\n"
        f"RAM: [{_bar(float(stats.get('ram', 0.0)))}] {float(stats.get('ram', 0.0)):.1f}%\n"
        f"CPU: [{_bar(float(stats.get('cpu', 0.0)))}] {float(stats.get('cpu', 0.0)):.1f}%\n"
        f"CON: {int(stats.get('con', 0))} | THR: {int(stats.get('thr', 0))} {_status_emoji(status)} {html.escape(status)}\n"
        f"OUTPUT: {html.escape(str(stats.get('output', 'No output')))}\n"
        "</pre>"
    )
    return text, device_menu_kb(device_id)
