import asyncio

from loguru import logger

from core.bash_utils import run_bash


class HealthSnapshot:
    __slots__ = ("ram_percent", "cpu_percent", "tcp_connections", "threads", "watchdog_status")

    def __init__(self, ram_percent: float, cpu_percent: float, tcp_connections: int, threads: int, watchdog_status: str):
        self.ram_percent = ram_percent
        self.cpu_percent = cpu_percent
        self.tcp_connections = tcp_connections
        self.threads = threads
        self.watchdog_status = watchdog_status


class SystemMonitor:
    def __init__(self, *, tcp_active_threshold: int = 8, tcp_zombie_threshold: int = 5, ram_critical_percent: float = 98.0) -> None:
        self.tcp_active_threshold = tcp_active_threshold
        self.tcp_zombie_threshold = tcp_zombie_threshold
        self.ram_critical_percent = ram_critical_percent
        self._last_cpu_total: int | None = None
        self._last_cpu_idle: int | None = None

    async def set_process_priority(self, pid: int) -> None:
        code, _, err = await run_bash(f"echo -1000 > /proc/{pid}/oom_score_adj", root=True)
        if code != 0:
            logger.warning(f"Failed to set oom_score_adj for PID {pid}: {err}")
        await run_bash(f"echo -17 > /proc/{pid}/oom_adj", root=True)
        code, _, err = await run_bash(f"renice -n -20 -p {pid}", root=True)
        if code != 0:
            logger.warning(f"Failed to set nice -20 for PID {pid}: {err}")

    async def reboot_now(self) -> None:
        logger.critical("CRITICAL RAM threshold hit. Executing reboot.")
        await run_bash("reboot", root=True)

    async def restart_roblox(self) -> None:
        cmd = "am force-stop com.roblox.client && monkey -p com.roblox.client -c android.intent.category.LAUNCHER 1"
        code, out, err = await run_bash(cmd, root=True, timeout=30)
        if code == 0:
            logger.warning("Watchdog restart executed for Roblox app.")
        else:
            logger.error(f"Failed to restart Roblox app: {err or out}")

    async def get_ram_percent(self) -> float:
        code, content, _ = await run_bash("cat /proc/meminfo", root=True)
        if code != 0:
            return 0.0
        mem_total = 0
        mem_available = 0
        for line in content.splitlines():
            if line.startswith("MemTotal:"):
                mem_total = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                mem_available = int(line.split()[1])
        if mem_total <= 0:
            return 0.0
        return max(0.0, min(100.0, ((mem_total - mem_available) / mem_total) * 100.0))

    async def get_cpu_percent(self) -> float:
        code, content, _ = await run_bash("cat /proc/stat", root=True)
        if code != 0 or not content:
            return 0.0
        first = content.splitlines()[0]
        parts = [int(x) for x in first.split()[1:8]]
        idle = parts[3] + parts[4]
        total = sum(parts)
        if self._last_cpu_total is None or self._last_cpu_idle is None:
            self._last_cpu_total = total
            self._last_cpu_idle = idle
            return 0.0
        delta_total = total - self._last_cpu_total
        delta_idle = idle - self._last_cpu_idle
        self._last_cpu_total = total
        self._last_cpu_idle = idle
        if delta_total <= 0:
            return 0.0
        return max(0.0, min(100.0, (1.0 - (delta_idle / delta_total)) * 100.0))

    async def get_clone_connections(self) -> int:
        code, out, _ = await run_bash("cat /proc/net/tcp | grep ' 01 ' | wc -l", root=True)
        if code != 0:
            return 0
        try:
            return max(0, int(out.strip()))
        except ValueError:
            return 0

    async def get_threads(self, pid: int) -> int:
        code, out, _ = await run_bash(f"cat /proc/{pid}/status | grep Threads", root=True)
        if code != 0:
            return 0
        try:
            return int(out.split(":", 1)[1].strip())
        except Exception:
            return 0

    async def snapshot(self, pid: int) -> HealthSnapshot:
        ram = await self.get_ram_percent()
        cpu = await self.get_cpu_percent()
        con = await self.get_clone_connections()
        thr = await self.get_threads(pid)
        if con >= self.tcp_active_threshold:
            status = "ACTIVE"
        elif con <= self.tcp_zombie_threshold:
            status = "ZOMBIE"
        else:
            status = "UNSTABLE"
        return HealthSnapshot(ram_percent=ram, cpu_percent=cpu, tcp_connections=con, threads=thr, watchdog_status=status)

    async def ram_guard_loop(self, interval_sec: int = 10) -> None:
        while True:
            try:
                ram = await self.get_ram_percent()
                if ram >= self.ram_critical_percent:
                    await self.reboot_now()
                await asyncio.sleep(interval_sec)
            except Exception as exc:
                logger.exception(f"ram_guard_loop error: {exc}")
                await asyncio.sleep(interval_sec)

    async def watchdog_loop(self, interval_sec: int = 15) -> None:
        while True:
            try:
                con = await self.get_clone_connections()
                if con <= self.tcp_zombie_threshold:
                    logger.warning(f"Watchdog ZOMBIE detected, con={con} <= {self.tcp_zombie_threshold}")
                    await self.restart_roblox()
                await asyncio.sleep(interval_sec)
            except Exception as exc:
                logger.exception(f"watchdog_loop error: {exc}")
                await asyncio.sleep(interval_sec)
