import asyncio
import os
import shlex


TERMUX_BIN = "/data/data/com.termux/files/usr/bin"
TERMUX_BASH = f"{TERMUX_BIN}/bash"


async def run_bash(command: str, root: bool = False, timeout: int = 20) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["PATH"] = "/data/data/com.termux/files/usr/bin:/data/data/com.termux/files/usr/bin/applets:/system/bin:/system/xbin"
    env["LD_LIBRARY_PATH"] = "/data/data/com.termux/files/usr/lib"

    bash_wrapped = f"{shlex.quote(TERMUX_BASH)} -c {shlex.quote(command)}"
    already_root = hasattr(os, "geteuid") and os.geteuid() == 0
    final_command = bash_wrapped if (not root or already_root) else f"su -c {shlex.quote(bash_wrapped)}"

    proc = await asyncio.create_subprocess_shell(
        final_command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
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
