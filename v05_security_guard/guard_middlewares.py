"""v05 核心零件：三枚安全中间件 —— 保险丝集中进配电箱。

参考本体
  packages/harness/deerflow/agents/middlewares/thread_data_middleware.py
      ThreadDataMiddleware：before_agent 按身份算好并创建线程工作目录，
      把路径写进 state["thread_data"]（声明即分配，工具永远假设有目录）。
  packages/harness/deerflow/sandbox/middleware.py
      SandboxMiddleware：沙箱生命周期绑定 (user, thread)，state["sandbox"]
      记 sandbox_id，同 thread 复用、不重复认领。
  packages/harness/deerflow/agents/middlewares/read_before_write_middleware.py
      ReadBeforeWriteMiddleware：wrap_tool_call 版本闸门——覆盖写已存在的
      文件，必须存在对该文件**当前版本**（sha256）的读取记录，否则伪造
      一条错误 ToolMessage 一票否决，工具根本不执行。

与 v04 的本质区别（本版全部教学重点）：
  v04：安全写在工具函数里——写错一次工具，安全就没了；
  v05：安全写在管道里——工具变笨（只剩干活的本事），护栏由框架统一安装。
  与 Web 开发"鉴权写中间件而不是每个 handler"是同一个思想。
"""

from __future__ import annotations

import hashlib
import posixpath
from typing import Any, NotRequired

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState
from langchain_core.messages import ToolMessage

from sandbox import SandboxProvider

READ_MARK_KEY = "dfl_read_mark"   # 本体同名机制：deerflow_read_mark

_BLOCK_MESSAGE = (
    "Error: {tool_name} blocked — {path} already exists and you have not read its "
    "current version. Any write invalidates earlier reads, so re-read before every "
    "modification. Call read_file, check what is there, then retry."
)


class LabState(AgentState):
    """图状态加两个私有键：线程数据（目录身份）与沙箱句柄（运行时身份）。"""

    thread_data: NotRequired[dict[str, Any] | None]
    sandbox: NotRequired[dict[str, Any] | None]


class ThreadDataMiddleware(AgentMiddleware):
    """① 分配：run 一开始就按身份建好目录，把身份登记进 state。

    本体从 runtime.context / get_config().configurable 取 thread_id，
    学习版走 configurable 一条通道（与 checkpointer 共用身份，见 main.py）。
    """

    state_schema = LabState

    def __init__(self, provider: SandboxProvider) -> None:
        super().__init__()
        self._provider = provider

    def before_agent(self, state: LabState, runtime: Any) -> dict[str, Any] | None:
        from langgraph.config import get_config

        configurable = get_config().get("configurable", {})
        user_id = configurable.get("user_id", "anon")
        thread_id = configurable.get("thread_id", "default")
        sbx = self._provider.get(user_id, thread_id)   # 触发目录创建（本体同语义）
        print(f"  >> [ThreadData] 分配工作目录 user={user_id} thread={thread_id}")
        return {"thread_data": {"user_id": user_id, "thread_id": thread_id,
                                "workspace_path": sbx.root}}


class SandboxMiddleware(AgentMiddleware):
    """② 认领：把沙箱身份绑定到本次 thread，写进 state["sandbox"]。

    本体默认 lazy_init=True（首次工具调用才 acquire，再经 wrap_tool_call
    用 Command 把懒分配固化进图状态）；学习版取 eager 路径（before_agent
    即认领），语义等价、省掉一半簿记。同 thread 复跑不重复认领——
    与本体"沙箱跨 turn 复用"一致。
    """

    state_schema = LabState

    def __init__(self, provider: SandboxProvider) -> None:
        super().__init__()
        self._provider = provider

    def before_agent(self, state: LabState, runtime: Any) -> dict[str, Any] | None:
        if state.get("sandbox"):
            return None                      # 同 thread 复跑：已认领（本体同语义）
        td = state.get("thread_data") or {}
        sandbox_id = f"{td.get('user_id', 'anon')}/{td.get('thread_id', 'default')}"
        print(f"  >> [Sandbox] 认领沙箱 sandbox_id={sandbox_id}")
        return {"sandbox": {"sandbox_id": sandbox_id}}


class ReadBeforeWriteMiddleware(AgentMiddleware):
    """③ 否决：wrap_tool_call 版本闸门（本体同款设计，去掉并发锁）。

    读标记盖在 read_file 的 ToolMessage.additional_kwargs 上，随消息一起
    被摘要截断——标记没了，闸门必然重新落下，永不出现"内容已不在上下文、
    闸门却还放行"的脱节。写不刷标记：任何写入改变文件哈希，早期读戳
    自然作废——改一次、重读一次，版本闸门的全部机关就是这个哈希相等。
    """

    state_schema = LabState

    def __init__(self, provider: SandboxProvider) -> None:
        super().__init__()
        self._provider = provider

    def wrap_tool_call(self, request, handler):
        name = request.tool_call.get("name")
        path = (request.tool_call.get("args") or {}).get("path")
        if name in ("write_file", "str_replace") and isinstance(path, str) and path:
            blocked = self._check_gate(request, name, path)
            if blocked is not None:
                return blocked               # 一票否决：handler 根本不被调用
            return handler(request)
        if name == "read_file" and isinstance(path, str) and path:
            result = handler(request)
            self._stamp_read_mark(request, result, path)   # 读到什么版本，就盖什么戳
            return result
        return handler(request)

    # -- 闸门 ----------------------------------------------------------

    def _check_gate(self, request, tool_name: str, path: str) -> ToolMessage | None:
        try:
            current = self._content_of(request, path)
        except FileNotFoundError:
            return None                      # 文件不存在：新建，无需读
        except Exception:
            return None                      # 越界等异常交给工具报错（本体 fail-open 同策）
        if self._latest_mark(request.state, path) == self._hash(current):
            return None                      # 读过当前版本 —— 放行
        print(f"  >> [ReadBeforeWrite] 否决未读先写: {path}")
        return ToolMessage(
            content=_BLOCK_MESSAGE.format(tool_name=tool_name, path=path),
            tool_call_id=str(request.tool_call.get("id", "")),
            name=tool_name,
            status="error",
        )

    # -- 读标记 ----------------------------------------------------------

    def _stamp_read_mark(self, request, result, path: str) -> None:
        if not isinstance(result, ToolMessage) or result.status == "error":
            return
        try:
            current = self._content_of(request, path)
        except Exception:
            return                           # 读不到内容就不盖章（本体同策）
        result.additional_kwargs[READ_MARK_KEY] = {
            "path": posixpath.normpath(path), "hash": self._hash(current),
        }

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _content_of(self, request, path: str) -> str:
        return self._sandbox_of(request).read_file(path)

    def _sandbox_of(self, request):
        td = request.state.get("thread_data") or {}
        return self._provider.get(td.get("user_id", "anon"), td.get("thread_id", "default"))

    @staticmethod
    def _latest_mark(state: dict, path: str) -> str | None:
        want = posixpath.normpath(path)
        for message in reversed(state.get("messages") or []):
            mark = (getattr(message, "additional_kwargs", None) or {}).get(READ_MARK_KEY)
            if isinstance(mark, dict) and mark.get("path") == want:
                return mark.get("hash")
        return None
