import asyncio
import os
import re

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
        code, out, _ = await run_bash("cat /proc/net/tcp | grep ' 01 ' | wc -l", root=True)
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
        # Step 1: Use dumpsys meminfo for Android-level accuracy
        code, out, err = await run_bash("dumpsys meminfo | grep 'Total RAM:'", root=True)
        mem_total_kb = 0.0
        mem_free_kb = 0.0

        if code == 0 and "Total RAM:" in out:
            try:
                # Format: Total RAM: 7,856,128K (7,672,000K free)
                match = re.search(r"Total RAM:\s+([\d,]+)K\s+\(([\d,]+)K\s+free\)", out)
                if match:
                    mem_total_kb = float(match.group(1).replace(",", ""))
                    mem_free_kb = float(match.group(2).replace(",", ""))
            except Exception:
                pass

        # Step 2: Fallback to /proc/meminfo if dumpsys failed
        if mem_total_kb <= 0:
            code, out, err = await run_bash("cat /proc/meminfo", root=True)
            if code == 0:
                for line in out.splitlines():
                    if line.startswith("MemTotal:"):
                        mem_total_kb = float(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        mem_free_kb = float(line.split()[1])
                    elif line.startswith("MemFree:") and mem_free_kb <= 0:
                        mem_free_kb = float(line.split()[1])

        # Step 3: Fix Kernel Glitch (e.g. 8,000,000 TB reporting)
        # If reported RAM > 64GB, use fallback 4096 MB
        if mem_total_kb > 64 * 1024 * 1024:
            logger.warning(f"Kernel glitch detected (RAM: {mem_total_kb} KB). Falling back to 4096 MB.")
            mem_total_kb = 4096 * 1024
            # We don't know the real free RAM, so we assume a safe value if mem_free_kb is also glitched
            if mem_free_kb > mem_total_kb:
                mem_free_kb = mem_total_kb * 0.2

        if mem_total_kb <= 0:
            return 0.0

        used_kb = mem_total_kb - mem_free_kb
        used_percent = (used_kb / mem_total_kb) * 100.0
        return max(0.0, min(100.0, used_percent))

    async def get_ram_cpu(self) -> tuple[float, float]:
        ram = await self.get_ram_percent()
        code, out, err = await run_bash("top -n 1 -b | grep com.roblox.client", root=True)
        if code != 0:
            logger.warning(f"get_ram_cpu top failed: {err or out}")
            return ram, 0.0

        cpu = 0.0
        try:
            # Accept either an explicit percent column (e.g. 12.3%) or Android top numeric CPU column.
            percent_match = re.search(r"([0-9]+(?:\.[0-9]+)?)%", out)
            if percent_match:
                cpu = float(percent_match.group(1))
            else:
                parts = out.split()
                for token in parts:
                    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", token):
                        value = float(token)
                        if 0.0 <= value <= 100.0:
                            cpu = value
                            break
            cpu = max(0.0, min(100.0, cpu))
        except Exception as exc:
            logger.warning(f"get_ram_cpu parse failed: {exc}")
            cpu = 0.0

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
            # Winning Launch Command Format for restarts
            restart_cmd = (
                "su -c \"am force-stop com.roblox.client && "
                "nohup am start -n com.roblox.client/com.roblox.client.startup.ActivitySplash > /dev/null 2>&1 &\""
            )
            code, out, err = await run_bash(restart_cmd, root=False)
            if code != 0:
                logger.warning(f"watchdog_tick restart failed: {err or out}")
                return False
            return True
        return False
