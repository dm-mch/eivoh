"""Linux per-turn subreaper. Invoked as a script by the Claude adapter.

Only this CLI and its descendants can be adopted here. Inherited stdio preserves
JSONL verbatim. Normal exit also cleans descendants, including pipe holders.
This is lifecycle containment, not a sandbox; SIGKILL of the supervisor itself
still requires systemd's cgroup cleanup.
"""
import ctypes
import os
import signal
import subprocess
import sys
import time


def enable_subreaper():
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot enable child subreaper")


def children():
    # Every direct child is ours, including children adopted between scans.
    # Raw descriptors instead of pathlib glob: the scan repeats until ECHILD.
    result, me = [], os.getpid()
    for entry in os.scandir('/proc'):
        if not entry.name.isdigit():
            continue
        try:
            fd = os.open(f'/proc/{entry.name}/stat', os.O_RDONLY)
            try:
                data = os.read(fd, 4096)
            finally:
                os.close(fd)
            if int(data[data.rindex(b')') + 1:].split()[1]) == me:
                result.append(int(entry.name))
        except (OSError, ValueError, IndexError):
            continue
    return result


def main():
    enable_subreaper()  # Explicit startup, never an adapter-constructor side effect.
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    grace = float(sys.argv[1])
    proc = subprocess.Popen(sys.argv[2:])
    deadline = None
    while proc.poll() is None:
        if stopping and deadline is None:
            proc.terminate()
            deadline = time.monotonic() + grace
        if deadline is not None and time.monotonic() >= deadline:
            proc.kill()
        time.sleep(0.01)
    code = proc.returncode
    # Kill direct children; their children are adopted here on exit. Iterate
    # until ECHILD. Unreaped direct children cannot have their PIDs reused.
    while True:
        for pid in children():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            time.sleep(0.01)
    return code if code >= 0 else 128 - code


if __name__ == '__main__':
    sys.exit(main())
