"""
runners.py
----------
Thin wrappers for running shell commands either locally (subprocess)
or on a remote host (paramiko SSH).

Both runners expose the same interface:
    runner.run(cmd, sudo=False) -> (stdout: str, stderr: str)
    runner.close()

This lets all collection functions work without caring whether
they are talking to a local or remote node.
"""

import os
import subprocess

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False


def run_local(cmd: str, sudo: bool = False) -> tuple:
    """Run a shell command on the local machine. Returns (stdout, stderr)."""
    if sudo and os.geteuid() != 0:
        cmd = f"sudo {cmd}"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=60)
        return r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT"
    except Exception as e:
        return "", str(e)


class LocalRunner:
    """Runs commands on the local machine via subprocess."""

    def __init__(self, hostname: str):
        self.host = hostname

    def run(self, cmd: str, sudo: bool = False) -> tuple:
        return run_local(cmd, sudo=sudo)

    def close(self):
        pass


class SSHRunner:
    """Runs commands on a remote host over SSH using paramiko."""

    def __init__(self, host: str, user: str,
                 key_path=None, password=None, port: int = 22):
        self.host = host
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = dict(hostname=host, username=user, port=port, timeout=30)
        if key_path:
            kw["key_filename"] = str(key_path)
        if password:
            kw["password"] = password
        self._client.connect(**kw)

    def run(self, cmd: str, sudo: bool = False) -> tuple:
        if sudo:
            cmd = f"sudo {cmd}"
        _, out, err = self._client.exec_command(cmd, timeout=60)
        return (out.read().decode("utf-8", errors="replace").strip(),
                err.read().decode("utf-8", errors="replace").strip())

    def close(self):
        self._client.close()
