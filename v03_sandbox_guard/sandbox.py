"""v03 核心零件一：LocalSandbox —— 给模型装手脚，再关进笼子。

参考本体 packages/harness/deerflow/sandbox/（local 版）。
三道机制：
1. 虚拟路径：模型只见 /mnt/user-data，真实路径（本版 data/sandbox/）永不出场；
2. 越界拒绝：解析后 realpath 不在沙箱根之下 -> 当场拒（/etc/passwd 进不来）；
3. 输出遮码：bash 输出里的真实路径遮回虚拟路径、注入的密钥值遮成 [redacted]。
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
from pathlib import Path

VIRTUAL = "/mnt/user-data"


class SandboxError(Exception):
    pass


class LocalSandbox:
    def __init__(self, root: Path, injected_secrets: dict[str, str]) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.secrets = injected_secrets
        self.read_stamps: dict[Path, str] = {}      # 文件 -> 最近一次读/写时的内容哈希

    # ---- 路径翻译：虚拟 <-> 真实 ----
    def to_actual(self, virtual_path: str) -> Path:
        if not (virtual_path == VIRTUAL or virtual_path.startswith(VIRTUAL + "/")):
            raise SandboxError(f"越界路径被拒：{virtual_path}（你只能见 {VIRTUAL}）")
        p = (self.root / virtual_path.removeprefix(VIRTUAL).lstrip("/")).resolve()
        if not p.is_relative_to(self.root):          # ../ 等手段解析后仍在根外 -> 拒
            raise SandboxError(f"越界路径被拒：{virtual_path}（你只能见 {VIRTUAL}）")
        return p

    def mask(self, text: str) -> str:
        text = text.replace(str(self.root), VIRTUAL)
        for value in self.secrets.values():
            text = text.replace(value, "[redacted]")
        return text

    # ---- 四个动作 ----
    def read(self, virtual_path: str) -> str:
        p = self.to_actual(virtual_path)
        if not p.is_file():
            raise SandboxError(f"文件不存在：{virtual_path}")
        text = p.read_text(encoding="utf-8")
        self.read_stamps[p] = hashlib.sha256(text.encode()).hexdigest()   # 盖读戳
        return text

    def write(self, virtual_path: str, content: str) -> str:
        p = self.to_actual(virtual_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        self.read_stamps[p] = hashlib.sha256(content.encode()).hexdigest()
        return f"写入 {len(content)} 字符 -> {virtual_path}"

    def current_hash(self, virtual_path: str) -> str | None:
        p = self.to_actual(virtual_path)
        if not p.is_file():
            return None
        return hashlib.sha256(p.read_bytes()).hexdigest()

    def stamp_of(self, virtual_path: str) -> str | None:
        return self.read_stamps.get(self.to_actual(virtual_path))

    def bash(self, command: str, timeout: float = 10) -> str:
        env = {**os.environ, **self.secrets}
        # start_new_session：命令连同它 spawns 的后台进程独立成组，
        # 超时时 killpg 一锅端（只杀父进程会漏掉 `sleep 25 &` 这种后台子进程）
        proc = subprocess.Popen(command, shell=True, cwd=self.root, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait()
            return self.mask(f"(超时 {timeout}s，整个进程组已击杀) ")
        return self.mask(f"exit={proc.returncode}\n{out}{err}")
