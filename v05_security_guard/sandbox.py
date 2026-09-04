"""v05 的沙箱本体 —— 参考本体 packages/harness/deerflow/sandbox/local/。

与 v04 版的关键区别：这是一个"变笨"的沙箱。
- write_file 不再自查 READ-BEFORE-WRITE（v04 的自觉已删除）；
- read_file 不再记账；
- 越界路径仍会在 _to_actual 抛 ValueError，但那是最后一道 fail-safe，
  真正的否决权在中间件（guard_middlewares.py）——工具甚至不知道自己
  被保护着。安全从"工具自觉"升级为"框架强制"。
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

VIRTUAL_PATH_PREFIX = "/mnt/user-data"        # 本体 config/paths.py 同名同值
DEFAULT_COMMAND_TIMEOUT_SECONDS = 600          # 本体同名常量


class LocalSandbox:
    """一个 (user_id, thread_id) 对应一个沙箱实例（本体同语义）。"""

    def __init__(self, user_id: str, thread_id: str, base_dir: Path) -> None:
        self.root = str(base_dir / "users" / user_id / "threads" / thread_id / "user-data")
        Path(self.root).mkdir(parents=True, exist_ok=True)

    def to_actual(self, virtual_path: str) -> str:
        """虚拟路径 -> 真实路径。越界/逃逸在此抛异常（最后的 fail-safe）。"""
        if not (virtual_path == VIRTUAL_PATH_PREFIX
                or virtual_path.startswith(VIRTUAL_PATH_PREFIX + "/")):
            raise ValueError(f"path outside sandbox: {virtual_path!r}")
        rel = virtual_path[len(VIRTUAL_PATH_PREFIX):].lstrip("/")
        full = os.path.normpath(os.path.join(self.root, rel))
        if full != self.root and not full.startswith(self.root + os.sep):
            raise ValueError(f"path traversal detected: {virtual_path!r}")
        return full

    def mask_local_paths_in_output(self, output: str) -> str:
        return output.replace(self.root, VIRTUAL_PATH_PREFIX)

    def read_file(self, path: str) -> str:
        return Path(self.to_actual(path)).read_text(encoding="utf-8", errors="replace")

    def write_file(self, path: str, content: str) -> str:
        actual = self.to_actual(path)
        Path(actual).parent.mkdir(parents=True, exist_ok=True)
        Path(actual).write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} chars to {path}"

    def execute_command(self, command: str,
                        timeout: int = DEFAULT_COMMAND_TIMEOUT_SECONDS) -> tuple[int, str]:
        proc = subprocess.Popen(["/bin/bash", "-lc", command], cwd=self.root,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=timeout)
            return proc.returncode, self.mask_local_paths_in_output(out or "")
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            out, _ = proc.communicate()
            return 124, self.mask_local_paths_in_output(out or "") + "\n[timeout: process group killed]"


class SandboxProvider:
    """按 (user_id, thread_id) 懒创建并缓存沙箱 —— 本体 LocalSandboxProvider 同款。"""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self._cache: dict[tuple[str, str], LocalSandbox] = {}

    def get(self, user_id: str, thread_id: str) -> LocalSandbox:
        key = (user_id, thread_id)
        if key not in self._cache:
            self._cache[key] = LocalSandbox(user_id, thread_id, self.base_dir)
        return self._cache[key]

    def clear(self) -> None:
        self._cache.clear()   # 磁盘重置后必须清缓存，防账本与磁盘脱节

    def instances(self) -> dict[tuple[str, str], LocalSandbox]:
        return dict(self._cache)
