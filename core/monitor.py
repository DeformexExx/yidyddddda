import asyncio
import os

import psutil
from loguru import logger

from core.bash_utils import run_bash


class SystemMonitor:
    def __init__(self, tcp_active_threshold: int = 8, tcp_zombie_threshold: int = 5, ram_critical_percent: float = 98.0) -> None:
        self.tcp_active_threshold = tcp_active_threshold
        self.tcp_zombie_threshold = tcp_zombie_threshold
        self.ram_critical_percent = ram_critical_percent

    async def set_process_priority(self, pid: int) -> None:
        await run_bash(f"echo -17 > /proc/{pid}/oom_adj", root=True)
        await run_bash(f"echo -1000 > /proc/{pid}/oom_score_adj", root=True)
        await run_bash(f"renice -n -20 -p {pid}", root=True)

    async def get_connections(self) -> int:
        code, out, _ = await run_bash("netstat -ant | grep ESTABLISHED | wc -l", root=True)
        if code != 0:
            return 0
        try:
            return int(out.strip())
        except Exception:
            return 0

    async def get_threads(self, pid: int) -> int:
        code, out, _ = await run_bash(f"cat /proc/{pid}/status | grep Threads | awk '{{print $2}}'", root=True)
        if code != 0:
            return 0
        try:
            return int(out.strip())
        except Exception:
            return 0

    async def get_ram_percent(self) -> float:
        return float(psutil.virtual_memory().percent)

    async def get_ram_cpu(self) -> tuple[float, float]:
        ram = await self.get_ram_percent()
        cpu = float(psutil.cpu_percent(interval=0.1))
        return ram, cpu

    async def snapshot(self, pid: int) -> dict[str, float | int | str]:
        ram, cpu = await self.get_ram_cpu()
        con = await self.get_connections()
        thr = await self.get_threads(pid)
        if con >= self.tcp_active_threshold:
            status = "ACTIVE"
        elif con <= self.tcp_zombie_threshold:
            status = "ZOMBIE"
        else:
            status = "UNSTABLE"
        return {"ram": ram, "cpu": cpu, "con": con, "thr": thr, "status": status}

    async def reboot_if_needed(self) -> None:
        ram, _ = await self.get_ram_cpu()
        if ram >= self.ram_critical_percent:
            logger.critical("RAM critical -> reboot")
            await run_bash("reboot", root=True)

    async def watchdog_tick(self) -> bool:
        con = await self.get_connections()
        if con <= self.tcp_zombie_threshold:
            await run_bash("am force-stop com.roblox.client && monkey -p com.roblox.client -c android.intent.category.LAUNCHER 1", root=True)
            return True
        return False
