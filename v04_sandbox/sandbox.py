"""v04 核心零件：LocalSandbox —— 模型的手脚，和关手脚的笼子。

参考本体 packages/harness/deerflow/sandbox/local/local_sandbox.py
      packages/harness/deerflow/sandbox/tools.py（mask_secret_values、
      _WRITE_FILE_CONTENT_MAX_BYTES、bash 工具）
      packages/harness/deerflow/sandbox/path_patterns.py（输出反向掩码）

保留本体的四件核心机制：
1. 虚拟路径：模型永远只见 /mnt/user-data/...，实际落在
   {base}/users/{uid}/threads/{tid}/user-data/...，最长前缀双向转换，
   越界与 ../ 逃逸在 _to_actual 里当场拒绝。
2. bash：start_new_session=True 起独立进程组；超时 os.killpg 杀整组
   （只杀 bash 不杀它起的子进程，等于没杀）。
3. READ-BEFORE-WRITE（自觉版）：覆盖已存在文件前必须先读过。注意这写在
   工具层——v05 会把它升级为框架强制。
4. 输出掩码：真实主机路径反向遮回虚拟路径；注入的密钥值替换成
   [redacted]（长度 <8 的值不遮，防误伤——本体 _MIN_MASK_LENGTH 同值）。
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

VIRTUAL_PATH_PREFIX = "/mnt/user-data"        # 本体 config/paths.py 同名同值
DEFAULT_COMMAND_TIMEOUT_SECONDS = 600          # 本体同名常量
WRITE_FILE_MAX_BYTES = 80 * 1024               # 本体 _WRITE_FILE_CONTENT_MAX_BYTES
SECRET_REDACTION = "[redacted]"                # 本体 _SECRET_REDACTION
MIN_MASK_LENGTH = 8                            # 本体 _MIN_MASK_LENGTH


class LocalSandbox:
    """一个 (user_id, thread_id) 对应一个沙箱实例（本体同语义）。"""

    def __init__(self, user_id: str, thread_id: str, base_dir: Path,
                 injected_secrets: dict[str, str] | None = None) -> None:
        self.virtual_to_actual = {
            VIRTUAL_PATH_PREFIX: str(base_dir / "users" / user_id / "threads" / thread_id / "user-data"),
        }
        self.actual_to_virtual = {v: k for k, v in self.virtual_to_actual.items()}
        Path(self.virtual_to_actual[VIRTUAL_PATH_PREFIX]).mkdir(parents=True, exist_ok=True)
        self.read_files: set[str] = set()          # READ-BEFORE-WRITE 账本（自觉版）
        self.secrets = dict(injected_secrets or {})

    # ------------------------------------------------ 路径双向转换
    def _to_actual(self, virtual_path: str) -> str:
        """最长前缀匹配 + 规范化 + 逃逸检查。越界在此拒绝，不留到落盘。"""
        best_prefix, best_actual = "", None
        for prefix, actual in self.virtual_to_actual.items():
            if virtual_path == prefix or virtual_path.startswith(prefix.rstrip("/") + "/"):
                if len(prefix) > len(best_prefix):
                    best_prefix, best_actual = prefix, actual
        if best_actual is None:
            raise ValueError(f"path outside sandbox: {virtual_path!r} (must start with {VIRTUAL_PATH_PREFIX})")
        rel = virtual_path[len(best_prefix):].lstrip("/")
        full = os.path.normpath(os.path.join(best_actual, rel))
        if full != best_actual and not full.startswith(best_actual + os.sep):
            raise ValueError(f"path traversal detected: {virtual_path!r}")
        return full

    def mask_local_paths_in_output(self, output: str) -> str:
        """反向掩码：真实主机路径 -> 虚拟路径（主机目录结构不外泄）。"""
        for actual, virtual in self.actual_to_virtual.items():
            output = output.replace(actual, virtual)
        return output

    def mask_secret_values(self, output: str) -> str:
        """密钥掩码：注入 env 的密钥值若被脚本 echo 出来，替换成 [redacted]。

        先遮长值再遮短值（长值是短值的超串时防部分泄漏）；短于
        MIN_MASK_LENGTH 的值不遮——两位地区码满天飞，误伤比泄漏更糟。
        """
        if not output:
            return output
        for value in sorted((v for v in self.secrets.values() if v and len(v) >= MIN_MASK_LENGTH),
                            key=len, reverse=True):
            output = output.replace(value, SECRET_REDACTION)
        return output

    # ------------------------------------------------ 文件操作
    def read_file(self, path: str) -> str:
        actual = self._to_actual(path)
        content = Path(actual).read_text(encoding="utf-8", errors="replace")
        self.read_files.add(actual)
        return self.mask_local_paths_in_output(content)

    def write_file(self, path: str, content: str, append: bool = False) -> str:
        if not append and len(content.encode("utf-8")) > WRITE_FILE_MAX_BYTES:
            return (f"Error: content is {len(content.encode('utf-8'))} bytes, over the "
                    f"{WRITE_FILE_MAX_BYTES} byte cap. Split into multiple append=True calls.")
        actual = self._to_actual(path)
        # READ-BEFORE-WRITE（v04 自觉版）：安全写在工具里。
        # 写错一次工具安全就没了——这正是 v05 要用中间件接管的东西。
        if Path(actual).exists() and not append and actual not in self.read_files:
            return ("Error: READ-BEFORE-WRITE — file exists but you have not read it this "
                    "session. Call read_file first so you overwrite the CURRENT content.")
        Path(actual).parent.mkdir(parents=True, exist_ok=True)
        with open(actual, "a" if append else "w", encoding="utf-8") as fh:
            fh.write(content)
        return f"Wrote {len(content)} chars to {path}"

    def str_replace(self, path: str, old: str, new: str) -> str:
        actual = self._to_actual(path)
        if actual not in self.read_files:
            return "Error: READ-BEFORE-WRITE — read the file first."
        text = Path(actual).read_text(encoding="utf-8")
        hits = text.count(old)
        if hits == 0:
            return "Error: old string not found."
        if hits > 1:
            return f"Error: old string appears {hits} times; provide more context."
        Path(actual).write_text(text.replace(old, new), encoding="utf-8")
        return f"Edited {path}"

    def list_dir(self, path: str) -> list[str]:
        actual = Path(self._to_actual(path))
        return sorted(str(p.relative_to(actual)) for p in actual.rglob("*") if p.is_file())

    # ------------------------------------------------ bash
    def execute_command(self, command: str,
                        timeout: int = DEFAULT_COMMAND_TIMEOUT_SECONDS) -> tuple[int, str]:
        """独立进程组执行；超时 killpg 杀整组；输出双掩码后回给模型。"""
        env = {**os.environ, **self.secrets}   # 密钥经 env 注入，绝不进命令串
        proc = subprocess.Popen(
            ["/bin/bash", "-lc", command],
            cwd=self.virtual_to_actual[VIRTUAL_PATH_PREFIX],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True,            # 独立进程组 => 可整组击杀
            env=env,
        )
        try:
            output, _ = proc.communicate(timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)   # 本体同款击杀
            except (ProcessLookupError, PermissionError):
                proc.kill()                    # 组已没了（竞态窗口）——补杀独苗
            output, _ = proc.communicate()
            output = (output or "") + "\nCommand timed out and was terminated. " \
                "Long-lived processes must run in background with output redirected."
            return 124, self.mask_local_paths_in_output(self.mask_secret_values(output))
        return code, self.mask_local_paths_in_output(self.mask_secret_values(output or ""))
