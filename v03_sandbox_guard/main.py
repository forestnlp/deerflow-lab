"""v03 · 沙箱与护栏 —— 给模型装上手脚，再给它建监狱。

运行（仓库根目录）：python -m v03_sandbox_guard.main

观察重点：
1. 模型只见 /mnt/user-data，真实路径、注入密钥、越界路径全被挡；
2. 护栏长在管道里（wrap_tool_call 否决 / after_model 剥爪），不在工具里；
3. 三组【机制自检】直接敲中间件和沙箱，不经过模型——信号 100% 确定；
   模型演示部分看的是终稿与落盘产物（措辞自由）。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage

from shared.model_factory import get_model
from v03_sandbox_guard.guards import LoopDetectionMiddleware, ReadBeforeWriteMiddleware
from v03_sandbox_guard.sandbox import SandboxError, LocalSandbox

DATA_DIR = Path(__file__).resolve().parent / "data"
SANDBOX_ROOT = DATA_DIR / "sandbox"
DEMO_SECRETS = {"DFL_DEMO_TOKEN": "sk-demo-0123456789abcdef"}    # 演示用假密钥


def build_tools(sbx: LocalSandbox):
    @tool
    def read_file(path: str) -> str:
        """读取虚拟路径文件的全文，path 形如 /mnt/user-data/outputs/weekly.md。"""
        try:
            return sbx.read(path)
        except SandboxError as e:
            return f"Error: {e}"

    @tool
    def write_file(path: str, content: str) -> str:
        """把 content 全文写入虚拟路径 path（覆盖写）。"""
        try:
            return sbx.write(path, content)
        except SandboxError as e:
            return f"Error: {e}"

    @tool
    def bash(command: str) -> str:
        """在沙箱根目录执行 shell 命令，输出中的敏感信息会被遮码。"""
        return sbx.bash(command)

    return [read_file, write_file, bash]


def model_demo(sbx: LocalSandbox, trace: list[str]) -> None:
    """真模型走完整流程：覆盖预置文件被拒 -> 先读 -> 再写。"""
    agent = create_agent(
        model=get_model(),
        tools=build_tools(sbx),
        middleware=[ReadBeforeWriteMiddleware(sbx, trace), LoopDetectionMiddleware(trace)],
    )
    ask = ("把 /mnt/user-data/outputs/weekly.md 里的增长率改成 12.3%。"
           "别绕弯子，直接把改好的全文 write_file 覆盖回去就行，不用先读。")
    print(f"[用户] {ask}\n")
    state = agent.invoke({"messages": [HumanMessage(ask)]})
    for t in trace:
        print(f"  >> {t}")
    weekly = SANDBOX_ROOT / "outputs" / "weekly.md"
    print(f"\n[终稿] {state['messages'][-1].content}")
    print(f"[落盘] {weekly.read_text(encoding='utf-8')!r}")


def guard_selfcheck(sbx: LocalSandbox, trace: list[str]) -> None:
    """不经模型，直接敲两个护栏——机制信号在这里 100% 确定。"""
    print("--- 自检① ReadBeforeWrite：伪造一次'未读先写'，看否决 ---")
    guard = ReadBeforeWriteMiddleware(sbx, trace)

    class FakeReq:                            # wrap_tool_call 只要这两样
        tool_call = {"name": "write_file", "args": {"path": "/mnt/user-data/outputs/weekly.md",
                                                    "content": "乱写"}, "id": "x1"}

    def never_called(request):                # handler 若被调用说明没拦住
        raise AssertionError("handler 被执行了——否决失败")

    msg = guard.wrap_tool_call(FakeReq(), never_called)
    print(f"  >> 否决返回: {msg.content[:52]}…（handler 未执行，工具没跑）\n")

    print("--- 自检② 输出遮码与越界拒绝（直接敲沙箱） ---")
    out = sbx.bash("echo token=$DFL_DEMO_TOKEN; pwd")
    print(f"  >> bash 输出: {out.strip()}")
    try:
        sbx.read("/etc/passwd")
    except SandboxError as e:
        print(f"  >> 越界读取: {e}")
    print()


def loop_selfcheck(trace: list[str]) -> None:
    """不经模型，喂五连重复点菜单给 LoopDetection，看剥爪。"""
    print("--- 自检③ LoopDetection：五连同一调用，剥爪强制交卷 ---")
    guard = LoopDetectionMiddleware(trace, warn=3, hard=5)
    same = AIMessage(content="", tool_calls=[
        {"name": "bash", "args": {"command": "date"}, "id": "c1"}])
    msgs = []
    state = {"messages": msgs}
    for _ in range(5):
        msgs.append(same)
        state["messages"] = list(msgs)
        patch = guard.after_model(state, None)
        if patch:
            msgs.append(patch["messages"][0])   # 顶掉后历史里已无爪子
            break
    for t in trace:
        print(f"  >> {t}")
    print(f"  >> stop_reason = {guard.stop_reason}；历史尾部消息带爪子吗："
          f"{bool(msgs[-1].tool_calls)}\n")


def main() -> None:
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sbx = LocalSandbox(root=SANDBOX_ROOT, injected_secrets=DEMO_SECRETS)
    print(f"虚拟路径 /mnt/user-data  =  {sbx.root}\n")

    # 预置旧文件：模型没读过它，第一次覆盖必被护栏否决
    pre = SANDBOX_ROOT / "outputs" / "weekly.md"
    pre.parent.mkdir(parents=True, exist_ok=True)
    pre.write_text("# 寄递业务周报\n上半年业务量同比增长 12%。", encoding="utf-8")

    guard_selfcheck(sbx, [])
    loop_selfcheck([])

    trace: list[str] = []
    model_demo(sbx, trace)

    print("\n--- 加餐：bash 超时连坐（直接敲沙箱，约 2 秒） ---")
    t0 = time.time()
    out = sbx.bash("sleep 25 & echo BG-started; sleep 25", timeout=2)
    print(f"  >> {out.strip()}（{time.time() - t0:.1f}s 返回）")


if __name__ == "__main__":
    main()
