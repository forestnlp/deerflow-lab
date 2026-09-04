# deerflow-lab —— 二十版造出一个 DeerFlow

> 一本**可以运行的书**：用开源 `langchain` / `langgraph`，从一次 LLM 调用出发，
> 二十个版本逐级搭出 DeerFlow 智能体能力的 95%。
> 每个版本目录都是**独立可运行项目**：不 import 兄弟目录，拷走就能跑。
> 每份 README 控制在 5 分钟读完：前置技术 → 原理 → 代码精读 → 运行 → 练习。

写作风格：严谨的骨架，当年明月的讲法，小灰的图解。历史告诉我们，
把"为什么"讲清楚的技术书，才有人读得完。

## 环境（conda，一次建好，二十版共用）

```bash
conda env create -f environment.yml      # 或：
conda create -n deerflow_lab python=3.12 && conda activate deerflow_lab
pip install -r requirements.txt

# 每个版本都能这样跑（在仓库根目录）：
conda run -n deerflow_lab python v01_react_loop/main.py --fake
```

- **`--fake` 模式**：零 API key、毫秒级、输出确定。讲义里说"你会看到 X"，就保证看到 X。
- **在线模式**：`export DFL_BASE_URL=... DFL_API_KEY=...` 后去掉 `--fake`。
  各版 `config.yaml` 用 `$VAR` 占位符读环境变量，密钥永不进文件。

## 版本地图（二十版，四环递进）

| 版 | 目录 | 本版新增 | 本体对应（backend/ 下） |
|---|---|---|---|
| **环一 · 让 agent 干活** ||||
| v01 | `v01_react_loop` | ReAct 循环 + 反射工厂（字符串→类） | `packages/harness/deerflow/agents/factory.py` |
| v02 | `v02_first_middleware` | 第一个中间件：钩子洋葱圈、工具调用合并 | `agents/middlewares/`（总论） |
| v03 | `v03_plan_mode` | write_todos、防提前退出、断点续做 | `middlewares/todo_middleware.py` |
| v04 | `v04_sandbox` | 虚拟路径、bash 沙箱、输出掩码、进程组击杀 | `sandbox/tools.py`、`sandbox/local/` |
| v05 | `v05_security_guard` | 安全进框架管道：ThreadData/READ-BEFORE-WRITE 一票否决 | `thread_data`/`sandbox`/`read_before_write` 中间件 |
| v06 | `v06_subagents` | task 工具、后台执行、并发闸、禁止套娃 | `tools/builtins/task_tool.py`、`subagents/` |
| **环二 · 让它靠得住** ||||
| v07 | `v07_title_memory` | 自动起标题；长期记忆（防抖抽取+旁路注入） | `title_`、`memory_middleware`、`agents/memory/` |
| v08 | `v08_context_control` | 摘要压缩管窗口，token 预算管账单 | `summarization_`/`token_budget_` |
| v09 | `v09_guardrails` | 循环检测、悬空调用修复、安全终止、澄清 | `loop_detection_`/`dangling_`/`safety_`/`clarification_` |
| v10 | `v10_llm_capabilities` | thinking/视觉/工具延迟加载/技能注入/文件引用 | `agents/llm_capabilities.py`、`skills/`、`uploads/` |
| **环三 · 让它成服务** ||||
| v11 | `v11_runtime_service` | RunManager、StreamBridge、SSE、断线续传 | `packages/communitydig/.../runtime/`、`gateway/` |
| v12 | `v12_persistence` | checkpoint 断点、SQLite run/thread 账本 | `runtime/persistence/` |
| v13 | `v13_gateway_auth` | FastAPI 网关、RBAC、CSRF、限流、模型权限 | `gateway/app.py`、`authz.py`、`auth_disabled.py` |
| v14 | `v14_channels` | 渠道总线、chat→thread 映射、去重、/命令 | `app/channels/message_bus.py`、`manager.py` |
| v15 | `v15_scheduler` | cron 解析、durable 任务表、skip/misfire 策略 | `scheduler/schedules.py` |
| v16 | `v16_mcp` | 手写 JSON-RPC over stdio 的 MCP 生态 | `mcp/tools.py`、`session_pool.py` |
| **环四 · 让它进生产** ||||
| v17 | `v17_observability` | run 事件流、trace、指标、成本核算 | `persistence/run_event`、`tracing/`、`metrics/` |
| v18 | `v18_packaging` | CLI、配置校验、按声明装配一切 | `client.py`、`config/`、`reflection` 语义 |
| v19 | `v19_channels_matrix` | 渠道抽象层：一渠道变 N 渠道、连通性自检 | `app/channels/*`、`connection_test/` |
| v20 | `v20_full_assembly` | 总装：按本体顺序全链装配，多入口汇一图 | `agents/lead_agent/agent.py::build_middlewares` |

递进一句话版：

```
v01 循环     LLM 会调工具了（地基）
 v02 钩子     中间件洋葱圈，横切关注点有了家
  v03 规划     会拆任务、不偷懒、能续做
   v04 沙箱    有手有脚，关进笼子
    v05 护栏    安全靠管道强制，不靠工具自觉
     v06 外包   大任务拆给子代理，管住并发
      v07 记忆   跨会话认识你，异步旁路不堵路
       v08 省钱   摘要管装得下，预算管花得起
        v09 刹车   转圈、悬空、说错话，统统接住
         v10 感知   会思考、能看图、按需取工具
          v11 服务   脚本 → run/流式/SSE 的服务形态
           v12 存盘   断电重启，账本和现场都在
            v13 大门   网关：鉴权、限流、多租户的地基
             v14 渠道   任何 IM 都能进来
              v15 闹钟   到点自己干活
               v16 生态   工具从别的进程里"长"出来
                v17 天眼   看得见每一步、算得清每一分钱
                 v18 装机   一条命令拉起整套
                  v19 矩阵   一个渠道抽象，八个渠道随便插
                   v20 总装   按本体接线图，全链合一
```

## 为什么说"95%"，剩下 5% 是什么

**在的（95%）**：ReAct+反射工厂、全部 30+ 中间件的**机制等价物**（顺序与本体一致）、
沙箱三件套、子代理、记忆/摘要/预算/护栏、run/流/断点三件套、渠道全机制（以微信为原型）、
调度全语义、MCP 协议等价物、鉴权/限流骨架、CLI、可观测。

**不在的（5%，刻意不写）**：Redis StreamBridge 与 Postgres 后端（内存/SQLite 同构替代，
换后端是改 2 行配置的事）、K8s/容器编排、8 渠道的具体第三方 API 适配（v14/v19 已给
完整抽象+一个全实现渠道）、企业级 authz 全量表、TUI、多 provider 修补
（thought_signature 那类胶水）、镜像构建脚本。

判断标准：**凡属"认知模型"的全部重做；凡属"运维胶水与第三方适配"的给同构替代**。
学完这二十版，你手里是地图和骨骼，不是标本。

## 每版目录构成（严格一致）

```
vNN_xxx/
├── README.md         5 分钟讲义：前置→原理→代码精读→运行→练习
├── main.py           可运行演示（--fake / 真实模型同一份代码）
├── *.py              本版新增零件（不 import 其他版本目录）
├── config.yaml       本版配置（密钥一律 $ENV 占位）
├── requirements.txt  本版依赖（通常三行）
└── data/             运行时自增目录（首次运行自动建，已 gitignore）
```

## 读法建议

1. 顺环读（v01→v20），每版 5 分钟讲义 + 1 分钟 `--fake` 实跑；
2. 或按岗位跳：做产品读 v01/03/07/14/15，做后端读 v05/11/12/13/17，做平台读 v16/18/19/20；
3. 每版讲义末尾有一道练习——**做练习才算读完这一版**。
