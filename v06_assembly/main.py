"""v06 · 总装 —— 一台机器，多个入口。

运行（仓库根目录）：python -m v06_assembly.main

观察重点：
1. 【装配清单】在真模型开口之前打印——这是不经模型的确定性信号；
2. CLI / 调度器 / IM 渠道三个入口只是三段不同的"外壳代码"，
   汇入同一个 agent + 同一个 SqliteSaver；机器只有一台，会话按 thread_id 隔离；
3. assert 只押机制信号（文件落盘、消息条数增长），不押模型措辞。

三幕：
幕① CLI 入口：写沙箱文件 + 计算器；after_agent 把长期记忆结算落盘。
幕② 调度器入口：无人值守，独立 thread_id 起会话；开局注入磁盘记忆（必现信号）。
幕③ IM 渠道入口：回到 CLI 同一 thread 续问——跨入口续聊靠 checkpointer，不靠外壳。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver

from v06_assembly.lead_agent import make_lead_agent

DATA = Path(__file__).resolve().parent / "data"
CKPT = DATA / "checkpoints.db"
CLI_THREAD = "im-main"        # CLI 与 IM 共用这个 thread（模拟同一会话两个前端）
CRON_THREAD = "cron-daily"    # 调度器每次触发用独立 thread


def entry_cli(agent, saver) -> int:
    """入口一：人在终端敲键盘。返回本 thread 结束时的消息条数。"""
    print("=== 幕① CLI 入口 ===")
    ask = ("记住：我负责的条线是寄递业务量。然后做两件事："
           "1) 把『上半年业务量同比增长12%』用 write_file 写入 /mnt/user-data/outputs/note.md；"
           "2) 用 calculator 算 12*1.5，把得数带进最终回答。")
    print(f"[CLI] {ask}\n")
    r = agent.invoke({"messages": [HumanMessage(ask)]},
                     config={"configurable": {"thread_id": CLI_THREAD}})
    print(f"[CLI] 终稿：{r['messages'][-1].content}\n")
    return len(r["messages"])


def entry_cron(agent, saver) -> None:
    """入口二：调度器无人值守触发——没有终端、没有人，代码就是『用户』。"""
    print("=== 幕② 调度器入口（cron 08:00 触发） ===")
    r = agent.invoke(
        {"messages": [HumanMessage("定时任务：read_file 读出 /mnt/user-data/outputs/note.md，"
                                   "然后一句话汇报文件内容。")]},
        config={"configurable": {"thread_id": CRON_THREAD}})
    print(f"[Cron] 汇报：{r['messages'][-1].content}\n")


def entry_im(agent, saver) -> int:
    """入口三：IM 渠道消息——回到幕①那个 thread 续问。"""
    print("=== 幕③ IM 渠道入口（同一 thread 续聊） ===")
    ask = "我之前说过我负责的条线是什么？上半年增长率是多少？只答这两点。"
    print(f"[IM] {ask}\n")
    r = agent.invoke({"messages": [HumanMessage(ask)]},
                     config={"configurable": {"thread_id": CLI_THREAD}})
    print(f"[IM] 终稿：{r['messages'][-1].content}\n")
    return len(r["messages"])


def main() -> None:
    shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True, exist_ok=True)

    with SqliteSaver.from_conn_string(str(CKPT)) as saver:
        agent, parts = make_lead_agent(
            DATA, checkpointer=saver,
            memory_seed=["用户负责的条线是寄递业务量"])
        n_cli = entry_cli(agent, saver)
        note = DATA / "sandbox" / "outputs" / "note.md"
        assert note.is_file() and "12%" in note.read_text(encoding="utf-8"), \
            "CLI 幕应当有文件落沙箱"
        memory = DATA / "memory.json"
        assert memory.is_file(), "after_agent 应当把长期记忆结算落盘"
        print(f"[自检] 沙箱文件、memory.json 均已落盘：{memory.read_text(encoding='utf-8')}\n")

        entry_cron(agent, saver)

        n_im = entry_im(agent, saver)
        assert n_im > n_cli, "IM 续聊应当接在同一 thread 的历史之后"
        last = agent.get_state({"configurable": {"thread_id": CLI_THREAD}})
        assert isinstance(last.values["messages"][-1], AIMessage)
        print(f"[assert] 同一 agent 三个入口：CLI {n_cli} 条 -> IM {n_im} 条，"
              f"跨入口续聊由 checkpointer 接管；调度器 thread 独立互不串台")


if __name__ == "__main__":
    main()
