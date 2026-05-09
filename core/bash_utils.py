import asyncio
import os
import shlex


TERMUX_PREFIX = "/data/data/com.termux/files/usr"
TERMUX_BIN = f"{TERMUX_PREFIX}/bin"
TERMUX_BASH = f"{TERMUX_BIN}/bash"
TERMUX_HOME = "/data/data/com.termux/files/home"


def get_termux_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{TERMUX_BIN}:{TERMUX_BIN}/applets:/system/bin:/system/xbin"
    env["LD_LIBRARY_PATH"] = f"{TERMUX_PREFIX}/lib"
    env["PREFIX"] = TERMUX_PREFIX
    env["HOME"] = TERMUX_HOME
    env["TERM"] = "xterm-256color"
    return env


def get_su_path() -> str:
    return f"{TERMUX_BIN}/su"


async def run_bash(command: str, root: bool = False, timeout: int = 20) -> tuple[int, str, str]:
    env = get_termux_env()
    final_command = command
    if root:
        final_command = f"{shlex.quote(get_su_path())} -c {shlex.quote(command)}"

    proc = await asyncio.create_subprocess_shell(
        final_command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        executable=TERMUX_BASH,
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
