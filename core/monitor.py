import asyncio
import shlex
from dataclasses import dataclass

from loguru import logger


@dataclass(slots=True)
class HealthSnapshot:
    ram_percent: float
    cpu_percent: float
    tcp_connections: int
    watchdog_status: str


class SystemMonitor:
    """
    Watchdog:
      ACTIVE -> TCP >= 8
      ZOMBIE -> TCP <= 5 (restart trigger)

    RAM policy:
      If RAM usage >= 98% => immediate reboot via `su -c reboot`.
    """

    def __init__(
        self,
        *,
        tcp_active_threshold: int = 8,
        tcp_zombie_threshold: int = 5,
        ram_critical_percent: float = 98.0,
    ) -> None:
        self.tcp_active_threshold = tcp_active_threshold
        self.tcp_zombie_threshold = tcp_zombie_threshold
        self.ram_critical_percent = ram_critical_percent
        self._last_cpu_total: int | None = None
        self._last_cpu_idle: int | None = None

    async def run_shell(self, command: str, *, root: bool = False, timeout: int = 20) -> tuple[int, str, str]:
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

    async def set_process_priority(self, pid: int) -> None:
        # Must be applied to bot process for maximum survivability.
        code, _, err = await self.run_shell(f"echo -1000 > /proc/{pid}/oom_score_adj", root=True)
        if code != 0:
            logger.warning(f"Failed to set oom_score_adj for PID {pid}: {err}")
        code, _, err = await self.run_shell(f"renice -n -20 -p {pid}", root=True)
        if code != 0:
            logger.warning(f"Failed to set nice -20 for PID {pid}: {err}")

    async def reboot_now(self) -> None:
        logger.critical("CRITICAL RAM threshold hit. Executing reboot.")
        await self.run_shell("reboot", root=True)

    async def restart_roblox(self) -> None:
        cmd = "am force-stop com.roblox.client && monkey -p com.roblox.client -c android.intent.category.LAUNCHER 1"
        code, out, err = await self.run_shell(cmd, root=True, timeout=30)
        if code == 0:
            logger.warning("Watchdog restart executed for Roblox app.")
        else:
            logger.error(f"Failed to restart Roblox app: {err or out}")

    async def get_ram_percent(self) -> float:
        mem_total = 0
        mem_available = 0
        content = await asyncio.to_thread(self._read_file, "/proc/meminfo")
        for line in content.splitlines():
            if line.startswith("MemTotal:"):
                mem_total = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                mem_available = int(line.split()[1])
        if mem_total <= 0:
            return 0.0
        used = mem_total - mem_available
        return max(0.0, min(100.0, (used / mem_total) * 100.0))

    async def get_cpu_percent(self) -> float:
        content = await asyncio.to_thread(self._read_file, "/proc/stat")
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
        usage = (1.0 - (delta_idle / delta_total)) * 100.0
        return max(0.0, min(100.0, usage))

    async def get_tcp_connection_count(self) -> int:
        # /proc/net/tcp + /proc/net/tcp6 minus headers
        tcp = await asyncio.to_thread(self._read_file, "/proc/net/tcp")
        tcp6 = await asyncio.to_thread(self._read_file, "/proc/net/tcp6")
        tcp_count = max(0, len(tcp.splitlines()) - 1)
        tcp6_count = max(0, len(tcp6.splitlines()) - 1)
        return tcp_count + tcp6_count

    async def get_watchdog_status(self) -> str:
        conns = await self.get_tcp_connection_count()
        if conns >= self.tcp_active_threshold:
            return "ACTIVE"
        if conns <= self.tcp_zombie_threshold:
            return "ZOMBIE"
        return "UNSTABLE"

    async def snapshot(self) -> HealthSnapshot:
        ram = await self.get_ram_percent()
        cpu = await self.get_cpu_percent()
        tcp = await self.get_tcp_connection_count()
        if tcp >= self.tcp_active_threshold:
            status = "ACTIVE"
        elif tcp <= self.tcp_zombie_threshold:
            status = "ZOMBIE"
        else:
            status = "UNSTABLE"
        return HealthSnapshot(ram_percent=ram, cpu_percent=cpu, tcp_connections=tcp, watchdog_status=status)

    async def ram_guard_loop(self, interval_sec: int = 10) -> None:
        while True:
            try:
                ram = await self.get_ram_percent()
                if ram >= self.ram_critical_percent:
                    logger.critical(f"RAM usage {ram:.2f}% >= {self.ram_critical_percent}%")
                    await self.reboot_now()
                await asyncio.sleep(interval_sec)
            except Exception as exc:
                logger.exception(f"ram_guard_loop error: {exc}")
                await asyncio.sleep(interval_sec)

    async def watchdog_loop(self, interval_sec: int = 15) -> None:
        while True:
            try:
                tcp_connections = await self.get_tcp_connection_count()
                if tcp_connections <= self.tcp_zombie_threshold:
                    logger.warning(
                        f"Watchdog ZOMBIE detected, tcp={tcp_connections} <= {self.tcp_zombie_threshold}. Restarting Roblox."
                    )
                    await self.restart_roblox()
                await asyncio.sleep(interval_sec)
            except Exception as exc:
                logger.exception(f"watchdog_loop error: {exc}")
                await asyncio.sleep(interval_sec)

    @staticmethod
    def _read_file(path: str) -> str:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
