from core.monitor import HealthSnapshot


def progress_bar(percent: float, length: int = 20) -> str:
    pct = max(0.0, min(100.0, percent))
    fill = int((pct / 100.0) * length)
    return "█" * fill + "░" * (length - fill)


def build_dashboard(device_name: str, snap: HealthSnapshot, active_cookie: str, active_server: str) -> str:
    tcp_status = "OK" if snap.watchdog_status == "ACTIVE" else snap.watchdog_status
    dashboard = (
        f"🛡 [{device_name}] SYSTEM DASHBOARD\n"
        "--------------------------\n"
        f"📈 RAM: [{progress_bar(snap.ram_percent)}] {snap.ram_percent:.0f}%\n"
        f"🧬 CPU: [{progress_bar(snap.cpu_percent)}] {snap.cpu_percent:.0f}%\n"
        f"🌐 CON: {snap.tcp_connections} | THR: {snap.threads} (Status: {tcp_status})\n"
        "--------------------------\n"
        f"🍪 Active: {active_cookie or 'None'}\n"
        f"🔗 Server: {active_server or 'None'}"
    )
    return f"```\n{dashboard}\n```"
