import asyncio
import os
import shlex


TERMUX_BIN = "/data/data/com.termux/files/usr/bin"
TERMUX_BASH = f"{TERMUX_BIN}/bash"


async def run_bash(command: str, root: bool = False, timeout: int = 20) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{TERMUX_BIN}:{TERMUX_BIN}/applets:" + env.get("PATH", "")

    # Keep LD_PRELOAD neutral unless already provided by host env.
    if "LD_PRELOAD" not in env:
        env["LD_PRELOAD"] = ""

    bash_wrapped = f"{shlex.quote(TERMUX_BASH)} -c {shlex.quote(command)}"
    final_command = f"su -c {shlex.quote(bash_wrapped)}" if root else bash_wrapped

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
