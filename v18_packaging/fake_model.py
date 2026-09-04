"""--fake 用的脚本模型与剧本注册 —— 作为"可反射装配的零件"存在。

注意 use 的写法：config.yaml 里 tools/middlewares 的 use 指向**实例或类**，
而 models 段 --fake 时指向本模块的 make_script_model 工厂。反射装配不关心
目标是类、函数还是现成实例（本体 reflection 也是 resolve_variable 一把梭）。
"""

from __future__ import annotations

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage


class ScriptChatModel(GenericFakeChatModel):
    """按剧本顺序吐消息的离线模型：bind_tools 忽略绑定，剧本耗尽即收尾。"""

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.outputs import ChatGeneration, ChatResult

        nxt = next(self.messages, None)
        if nxt is None:
            nxt = AIMessage(content="（fake 剧本耗尽：正常收尾）")
        return ChatResult(generations=[ChatGeneration(message=nxt)])


def make_script_model(script: str = "demo", **_ignored):
    """工厂入口：config.yaml 用 use: "fake_model:make_script_model" 装配它。

    剧本名 -> 消息序列。演示"配置选模型"：改一行配置就换了一个'模型'。
    """
    from scripts_library import SCRIPTS

    msgs = [m if isinstance(m, AIMessage) else AIMessage(content=m) for m in SCRIPTS[script]]
    return ScriptChatModel(messages=iter(msgs))


def tool_call(call_id: str, name: str, **args) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])
