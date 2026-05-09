import asyncio
import shlex


async def run_bash(command: str, root: bool = False, timeout: int = 20) -> tuple[int, str, str]:
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
