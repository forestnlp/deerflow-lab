# deerflow-lab —— 六版读完 DeerFlow 的骨架

> 一本用代码写的讲义：从一个手写 while 循环，到一台按本体图纸总装的 Lead Agent。
> 每一版都跑**真实模型**，每一个信号都可复现。

## 这个项目是什么

[DeerFlow](../deer-flow) 是一个生产级深度研究 Agent 框架，本体代码量大、
中间件多达三十来个，直接读容易迷路。本 lab 把它最核心的骨架拆成 **6 版**，
每版一个可独立运行的程序 + 一份 README 讲义：

| 版本 | 主题 | 一句话 |
|---|---|---|
| [v01_react_loop](v01_react_loop/README.md) | 手写 ReAct 循环 | 所谓 agent 框架，拆开就是 while + tool_calls |
| [v02_middleware_plan](v02_middleware_plan/README.md) | 中间件与规划 | create_agent 的洋葱圈钩子；write_todos 防提前交卷 |
| [v03_sandbox_guard](v03_sandbox_guard/README.md) | 沙箱与护栏 | 虚拟路径笼子 + 未读先写否决 + 循环剥爪 |
| [v04_context_memory](v04_context_memory/README.md) | 上下文与记忆 | 摘要折叠、token 预算硬停、跨对话长期记忆 |
| [v05_service](v05_service/README.md) | 服务化 | SqliteSaver 断电续聊 + FastAPI SSE 断线续传 |
| [v06_assembly](v06_assembly/README.md) | 总装 | 按本体 build_middlewares 同款顺序总装 + 多入口单机器 |

版本是**自然切分**出来的：一版解决一类问题，零件在后续版本里被反复复用
（v06 的整机全部零件来自 v02–v05），没有为凑数量而切的版。

## 两条铁律

1. **只跑真模型，没有 fake。** 每版 README 的"跑起来"一节分两栏：
   - **机制信号**——不经模型、直接敲中间件的自检 + assert 硬约束，每次必现；
   - **实录参考**——某次真跑的存档，你的模型措辞会不同，但信号行和终态一致。
2. **每处"同款本体"的说法都可查证。** 讲义引用的本体路径均为
   `deer-flow/backend/packages/harness/deerflow/...` 下的真实文件与行号；
   每版 README 末尾有"与本体差异（诚实声明）"，学习版偷懒的地方一律明说。

## 环境

```bash
conda create -n deerflow_lab python=3.12 -y
conda run -n deerflow_lab pip install -r requirements.txt
cp config.example.yaml config.yaml      # 填入你的模型网关（api_key / base_url）
```

模型配置语义抄自本体：`config.yaml` 的 models 段每条
`{name, use: "module:attr", ...构造参数}`，[shared/model_factory.py](shared/model_factory.py)
用反射装配（本体同款，见 `models/factory.py`）。`$VAR` 占位符展开自环境变量。

所有演示都在**仓库根目录**运行：

```bash
conda run -n deerflow_lab python -m v01_react_loop.main   # v02…v06 同理
```

## 推荐阅读姿势

- 顺序读：v01 → v06，每版先跑再读 README（各版 README 的"前情提要"会接上回）。
- 每版 README 结构固定：钩子 → 前情提要 → 前置技术 → 原理(图) → 代码精读 →
  跑起来(信号/实录) → Python 小课堂 → 与市面对比 → 与本体差异 → 练习 → 下一步。
- 读完 v06 后直接去读本体
  `agents/lead_agent/agent.py` 的 `build_middlewares`——你会发现大半名字都认识。

## 附录：本体有、本书砍掉的机制

六版篇幅装不下整台机器。以下机制本体怎么做、你在哪能看到，一张表交代清楚
（这些都是真的生产需求，不是装饰）：

| 机制 | 本体位置（harness/deerflow/ 下） | 做什么 | 本书为何砍 |
|---|---|---|---|
| ThreadData/Uploads | `agents/middlewares/thread_data_middleware.py`、`uploads/` | 每会话文件区与上传件管理，多租户隔离 | v03 单用户单沙箱已够演示护栏 |
| DynamicContext | `agents/middlewares/dynamic_context_middleware.py` | 日期/记忆以 `<system-reminder>` 注入首条 HumanMessage，system prompt 全静态吃前缀缓存 | v04 的 `HumanMessage(name=...)` 注入是它的简化版，同一手法 |
| Skills | `skills/`、`skill_activation/skill_tool_policy` 两件 | 斜杠命令确定性装载 SKILL.md；技能限定工具白名单 | 属"业务能力包"分发机制，与骨架正交 |
| DurableContext / TokenUsage / Title | 同名 middleware 文件 | 跨压缩存活的事实位；用量计费；首轮后自动起标题 | 记账与锦上添花，机制同 v04 |
| ViewImage | `view_image_middleware.py` | vision 模型专用，注入图片描述 | 学习网关无 vision |
| MCP 路由 + DeferredToolFilter | `mcp/`、`mcp_routing/deferred_tool_filter` 两件 | 外部 MCP 工具按需提升 schema、延迟注入省 token | 外部工具生态接入层，读者有 v01 工具协议基础后可直接读本体 |
| SystemMessageCoalescing | `system_message_coalescing_middleware.py` | 链尾合并相邻 system 消息，伺候严格网关 | 本书 v04 用 `name=` 通道避开了同一坑，本体正面修 |
| SubagentLimit / subagents | `subagents/`、`subagent_limit_middleware.py` | 子 agent 派生并发与深度限制 | 递归编排是另一本书 |
| TerminalResponse / SafetyFinishReason / ModelLengthFinish | 同名文件 | 空答复重试、安全终止掐爪、length 截断打标 | 都是终态防御的边角料，本体注释讲得清 |
| Clarification | `clarification_middleware.py`（永远最后） | 模型反问用户时挂起等待回答 | 交互式挂起依赖服务层(v05)之上的协议，超篇幅 |
| 渠道网关（IM 等） | `integrations/` | 飞书/Slack 等渠道外壳 | v06 幕③已演示"渠道只是翻译层" |
| 调度器 | `scheduler/` | 持久化 cron、错过补偿 | v06 幕②已演示"cron 只是另一个入口" |
| RBAC / authz | `authz/`、guardrails | 工具级授权、审计 | 企业边界，与安全扫描类读物更配 |
| 可观测性 | `tracing/`、`logging_config.py`、`trace_context.py` | run 级 trace 与结构化日志 | 任何框架都要接，无 DeerFlow 特色 |
| 沙箱容器版 | `sandbox/`（docker） | 真容器隔离替代 v03 的进程组连坐 | 学习版演示三道机制即可，原理同款 |

砍掉的标准只有一条：**是否影响理解"这台机器为什么这样转"**。
比如循环检测、未读先写否决、摘要折叠是"转法"本身，进正文；
Title 生成是"转速表"，进附录。

## 仓库约定

- `shared/`：跨版公用件（目前只有模型工厂）。
- `vNN_xxx/`：每版代码 + README；`vNN_xxx/data/` 为运行产物（gitignore）。
- `config.yaml` 本地真实配置（gitignore）；`config.example.yaml` 是模板。
- 依赖版本钉在 `requirements.txt`（langchain 1.x / langgraph 1.x 系）。
