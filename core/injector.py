import asyncio
import shlex
from dataclasses import dataclass

from loguru import logger


@dataclass(slots=True)
class InjectionResult:
    success: bool
    message: str


class InjectionEngine:
    """
    Roblox cookie injection into Chromium WebView cookie DB.
    Keeps root permission flow and file ownership/permission reset.
    """

    COOKIE_DB_PATH = "/data/data/com.roblox.client/app_webview/Default/Cookies"

    def __init__(self, roblox_uid: str = "10167", roblox_gid: str = "10167") -> None:
        self.roblox_uid = roblox_uid
        self.roblox_gid = roblox_gid

    async def run_shell(self, command: str, *, root: bool = False, timeout: int = 30) -> tuple[int, str, str]:
        final_command = f"su -c {shlex.quote(command)}" if root else command
        logger.debug(f"Exec: {final_command}")

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

        out = stdout.decode("utf-8", errors="ignore").strip()
        err = stderr.decode("utf-8", errors="ignore").strip()
        return proc.returncode or 0, out, err

    async def _sqlite_exec(self, sql: str) -> tuple[int, str, str]:
        escaped_sql = sql.replace("\"", "\\\"")
        cmd = f'sqlite3 {shlex.quote(self.COOKIE_DB_PATH)} "{escaped_sql}"'
        return await self.run_shell(cmd, root=True)

    async def inject_cookie(self, roblosecurity_cookie: str) -> InjectionResult:
        if not roblosecurity_cookie.strip():
            return InjectionResult(False, "Cookie is empty")

        # Step 1: Stop app to release DB lock.
        code, out, err = await self.run_shell("am force-stop com.roblox.client", root=True)
        if code != 0:
            logger.warning(f"force-stop failed: {err or out}")

        # Step 2: Ensure DB exists.
        code, out, err = await self.run_shell(f"test -f {shlex.quote(self.COOKIE_DB_PATH)}", root=True)
        if code != 0:
            return InjectionResult(False, f"Cookie DB not found: {self.COOKIE_DB_PATH}")

        # Step 3: Relax permissions temporarily for sqlite write.
        perm_cmd = (
            f"chown {self.roblox_uid}:{self.roblox_gid} {shlex.quote(self.COOKIE_DB_PATH)} && "
            f"chmod 660 {shlex.quote(self.COOKIE_DB_PATH)}"
        )
        code, out, err = await self.run_shell(perm_cmd, root=True)
        if code != 0:
            return InjectionResult(False, f"Failed to set DB permissions: {err or out}")

        domain = ".roblox.com"
        name = ".ROBLOSECURITY"
        value = roblosecurity_cookie.replace("'", "''")

        delete_sql = (
            "DELETE FROM cookies "
            f"WHERE host_key='{domain}' AND name='{name}';"
        )

        insert_sql = (
            "INSERT INTO cookies "
            "(creation_utc, host_key, top_frame_site_key, name, value, encrypted_value, path, "
            "expires_utc, is_secure, is_httponly, last_access_utc, has_expires, is_persistent, "
            "priority, samesite, source_scheme, source_port, is_same_party) "
            "VALUES "
            "(strftime('%s','now')*1000000, '.roblox.com', '', '.ROBLOSECURITY', '"
            f"{value}"
            "', X'', '/', 253402300799000000, 1, 1, strftime('%s','now')*1000000, 1, 1, 1, 0, 2, 443, 0);"
        )

        code, out, err = await self._sqlite_exec(delete_sql)
        if code != 0:
            return InjectionResult(False, f"DELETE failed: {err or out}")

        code, out, err = await self._sqlite_exec(insert_sql)
        if code != 0:
            return InjectionResult(False, f"INSERT failed: {err or out}")

        # Step 4: Restore restrictive ownership/permission after write.
        restore_cmd = (
            f"chown {self.roblox_uid}:{self.roblox_gid} {shlex.quote(self.COOKIE_DB_PATH)} && "
            f"chmod 600 {shlex.quote(self.COOKIE_DB_PATH)}"
        )
        code, out, err = await self.run_shell(restore_cmd, root=True)
        if code != 0:
            logger.warning(f"restore perms failed: {err or out}")

        return InjectionResult(True, "Cookie injected successfully")
