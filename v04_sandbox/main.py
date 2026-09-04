"""v04 · 沙箱 —— 给模型装上手脚，再关进笼子。

运行：
    conda run -n deerflow_lab python v04_sandbox/main.py --fake
    # 在线：export DFL_BASE_URL=... DFL_API_KEY=... 后去掉 --fake

观察重点：
1. 模型只见 /mnt/user-data，产物落在本版 data/sandbox/ 下（虚拟路径映射）；
2. 覆盖写被 READ-BEFORE-WRITE（自觉版，长在工具里）拒绝，先读后写才放行；
3. bash 输出里的真实主机路径被反向遮回虚拟路径；注入 env 的密钥被遮成
   [redacted]；越界路径（/etc/passwd）当场被拒；
4. 加餐演示：bash 超时 2 秒，os.killpg 把后台子进程一起连坐击杀。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import time
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage

from model_factory import fake_model, get_model, tool_call
from sandbox import LocalSandbox
from tools import build_tools

DATA_DIR = Path(__file__).resolve().parent / "data"
SANDBOX_ROOT = DATA_DIR / "sandbox"
DEMO_SECRETS = {"DFL_DEMO_TOKEN": "sk-demo-0123456789abcdef"}  # 学习版自拟演示密钥


def reset_data_dir() -> None:
    """运行时产物只写本版 data/，启动时清空（保留 .gitkeep 占位）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for p in DATA_DIR.iterdir():
        if p.name != ".gitkeep":
            shutil.rmtree(p) if p.is_dir() else p.unlink()


REPORT = "# 寄递业务周报\n上半年业务量同比增长 12%。"


def fake_script() -> list:
    """写周报 -> 覆盖被拒 -> 先读 -> 改数字 -> bash 统计(路径掩码) -> echo 密钥(掩码) -> 越界被拒。"""
    return [
        tool_call("t1", "write_file", description="创建周报",
                  path="/mnt/user-data/outputs/weekly.md", content=REPORT),
        tool_call("t2", "write_file", description="直接覆盖（应被拒）",
                  path="/mnt/user-data/outputs/weekly.md", content="# 覆盖版"),
        tool_call("t3", "read_file", description="先读当前内容",
                  path="/mnt/user-data/outputs/weekly.md"),
        tool_call("t4", "str_replace", description="修正数字",
                  path="/mnt/user-data/outputs/weekly.md", old="12%", new="12.3%"),
        tool_call("t5", "bash", description="统计字节数并看当前目录",
                  command="wc -c outputs/weekly.md && pwd"),
        tool_call("t6", "bash", description="打印环境变量里的令牌",
                  command="echo token=$DFL_DEMO_TOKEN"),
        tool_call("t7", "read_file", description="越界读取（应被拒）", path="/etc/passwd"),
        AIMessage("周报已生成并修订；密钥与主机路径已掩码，越界读取被沙箱拦截。"),
    ]


def show(result: dict) -> None:
    for m in result["messages"]:
        calls = getattr(m, "tool_calls", None)
        if calls:
            print(f"  [{type(m).__name__}] -> {[c['name'] for c in calls]}")
        else:
            text = m.content if isinstance(m.content, str) else str(m.content)
            text = text.replace("\n", "\\n")
            if len(text) > 84:
                text = text[:84] + "…"
            print(f"  [{type(m).__name__}] {text}")


def killpg_demo(sbx: LocalSandbox) -> None:
    """起一个带后台子进程的超时命令，验证 killpg 连后台一起杀。"""
    code, out = sbx.execute_command("sleep 25 & echo BG=$!; sleep 25", timeout=2)
    m = re.search(r"BG=(\d+)", out)
    bg_pid = m.group(1) if m else None
    gone = False
    if bg_pid:
        for _ in range(20):                     # 最多等 1 秒：等内核收尸
            r = subprocess.run(["ps", "-o", "state=", "-p", bg_pid],
                               capture_output=True, text=True)
            if r.returncode != 0 or "Z" in r.stdout:   # 没了，或只剩待收的僵尸
                gone = True
                break
            time.sleep(0.05)
    print(f"  [killpg 演示] exit={code} 超时通知={'timed out' in out} "
          f"后台子进程连坐消失={gone}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake", action="store_true", help="离线脚本模型，零 API key")
    args = ap.parse_args()

    reset_data_dir()
    sbx = LocalSandbox(user_id="demo-user", thread_id="thread-001",
                       base_dir=SANDBOX_ROOT, injected_secrets=DEMO_SECRETS)
    root = Path(__file__).resolve().parent
    print(f"虚拟路径 /mnt/user-data  =  本版目录/{{{Path(sbx.virtual_to_actual['/mnt/user-data']).relative_to(root)}}}")

    model = fake_model(fake_script()) if args.fake else get_model()
    agent = create_agent(model=model, tools=build_tools(sbx))
    prompt = "把『上半年寄递业务量增长 12%』写成周报文件，修订为 12.3%，用 bash 看字节数与目录，最后读 /etc/passwd 试试。"
    print(f"[用户] {prompt}\n")
    state = agent.invoke({"messages": [HumanMessage(content=prompt)]})
    show(state)

    print("\n--- 加餐 · bash 超时与进程组击杀（不经过模型，直接敲沙箱） ---")
    killpg_demo(sbx)

    print("\n产物落盘（真实路径，模型永远看不到这些）:")
    for p in sorted(Path(sbx.virtual_to_actual["/mnt/user-data"]).rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(SANDBOX_ROOT)}")


if __name__ == "__main__":
    main()
