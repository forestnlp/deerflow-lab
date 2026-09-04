"""v05 · 安全护栏 —— 从"工具自觉"升级为"框架强制"。

运行：
    conda run -n deerflow_lab python v05_security_guard/main.py --fake
    # 在线：export DFL_BASE_URL=... DFL_API_KEY=... 后去掉 --fake

五个步骤，全部走同一套管道（三枚中间件 + 三个笨工具）：
1. alice 新建文件——不存在，闸门不拦；
2. alice 直接覆盖——没有读取记录，wrap_tool_call 一票否决（工具没执行）；
3. alice 先读再写——读戳（sha256）对上当前版本，放行；
4. alice 写完再改——写入改变了文件哈希，旧读戳作废，又被拦；
5. bob 同路径新建——那是他的沙箱，文件不存在，直接放行（身份隔离）。
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from guard_middlewares import LabState, ReadBeforeWriteMiddleware, SandboxMiddleware, ThreadDataMiddleware
from model_factory import fake_model, get_model, tool_call
from sandbox import SandboxProvider
from tools import build_tools

DATA_DIR = Path(__file__).resolve().parent / "data"
PROVIDER = SandboxProvider(DATA_DIR / "sandbox")


def reset_data_dir() -> None:
    """运行时产物只写本版 data/，启动时清空（保留 .gitkeep 占位）。"""
    if DATA_DIR.exists():
        for p in DATA_DIR.iterdir():
            if p.name != ".gitkeep":
                shutil.rmtree(p) if p.is_dir() else p.unlink()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROVIDER.clear()   # 磁盘重置后丢掉缓存实例，防账本与磁盘脱节


def run_step(saver: MemorySaver, make_model, thread: str, user: str, ask: str,
             script: list, label: str) -> str:
    model = make_model(script)
    agent = create_agent(
        model=model,
        tools=build_tools(PROVIDER),
        middleware=[ThreadDataMiddleware(PROVIDER), SandboxMiddleware(PROVIDER),
                    ReadBeforeWriteMiddleware(PROVIDER)],
        state_schema=LabState,
        checkpointer=saver,          # 同一 thread 跨 run 续历史（本体同语义）
    )
    # 身份走 configurable：checkpointer 用它定位存档，中间件从 get_config() 认领沙箱
    state = agent.invoke({"messages": [HumanMessage(ask)]},
                         config={"configurable": {"thread_id": thread, "user_id": user}})
    last_tool = next(m for m in reversed(state["messages"]) if m.type == "tool")
    text = last_tool.content.replace("\n", "\\n")
    if len(text) > 72:
        text = text[:72] + "…"
    print(f"[步骤 {label}]")
    print(f"  [用户] {ask}")
    print(f"  [最后一条工具结果] {text}")
    return last_tool.content


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake", action="store_true", help="离线脚本模型，零 API key")
    args = ap.parse_args()

    reset_data_dir()
    make_model = (lambda s: fake_model(s)) if args.fake else (lambda s: get_model())
    saver = MemorySaver()

    # 步骤 1：新建（文件不存在 -> 闸门放行）
    run_step(saver, make_model, "t-a", "alice",
             "把『H1 收入 42.1 亿』写入 /mnt/user-data/notes.md",
             [tool_call("w1", "write_file", description="建文件",
                        path="/mnt/user-data/notes.md", content="H1 收入 42.1 亿"),
              AIMessage("已写入。")],
             "1 · alice 新建 —— 不存在，放行")

    # 步骤 2：直接覆盖（历史里无 read 戳 -> 中间件一票否决，工具不执行）
    run_step(saver, make_model, "t-a", "alice",
             "把 notes.md 直接改成『全年收入 90 亿』，不用读。",
             [tool_call("w2", "write_file", description="直接覆盖",
                        path="/mnt/user-data/notes.md", content="全年收入 90 亿"),
              AIMessage("被要求先读。")],
             "2 · alice 未读先写 —— 否决")

    # 步骤 3：先读再写（读戳哈希 == 当前版本 -> 闸门放行）
    run_step(saver, make_model, "t-a", "alice",
             "先读 notes.md，再改成『全年收入 90 亿』。",
             [tool_call("r1", "read_file", description="先读", path="/mnt/user-data/notes.md"),
              tool_call("w3", "write_file", description="读过再改",
                        path="/mnt/user-data/notes.md", content="全年收入 90 亿"),
              AIMessage("已修订。")],
             "3 · alice 先读后写 —— 放行")

    # 步骤 4：写完再改（写入已改变哈希，旧读戳作废 -> 又拦）
    run_step(saver, make_model, "t-a", "alice",
             "再把 notes.md 改成『全年收入 95 亿』。",
             [tool_call("w4", "write_file", description="再改一次",
                        path="/mnt/user-data/notes.md", content="全年收入 95 亿"),
              AIMessage("需要再读一次，收到。")],
             "4 · alice 写完再改 —— 版本变了，又否决")

    # 步骤 5：换人换 thread（bob 的沙箱里该路径不存在 -> 直接写）
    run_step(saver, make_model, "t-b", "bob",
             "在我的 /mnt/user-data/notes.md 写入『bob 的备忘』。",
             [tool_call("w5", "write_file", description="bob 建文件",
                        path="/mnt/user-data/notes.md", content="bob 的备忘"),
              AIMessage("完成。")],
             "5 · bob 同路径新建 —— 各关各的笼子，放行")

    print("\n沙箱仓库（按身份隔离，真实路径）:")
    for (user, thread), sbx in sorted(PROVIDER.instances().items()):
        rel = Path(sbx.root).relative_to(DATA_DIR / "sandbox")
        files = sorted(p.name for p in Path(sbx.root).rglob("*") if p.is_file())
        print(f"  {user}/{thread} -> data/sandbox/{rel}  files={files}")


if __name__ == "__main__":
    main()
