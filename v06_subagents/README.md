# v06 · 子代理：把大任务外包出去，再管住外包公司

> 一个人干不完的活，请帮手；帮手还得立规矩。

## 前情提要

v05 把安全闸门装进了中间件管道，agent 的单兵作战能力已经配齐：会规划（v03）、有手脚（v04）、不越界（v05）。但单兵终究是三头六臂——一份二十页的材料要精读、一份报告要撰写、还都得排队等同一个模型一轮一轮地转。这一版请出 task 工具：主 agent 把边界清楚的子任务外包给"专职子代理"，并给外包公司上三道规矩——并发闸、受限工具集、禁止套娃。

## 前置技术

- **create_agent 可以无限个**：agent 不是单例。v01 那行 `create_agent(model=..., tools=...)` 在运行时再执行一遍，就是一个全新的、与主对话完全隔离的 agent。
- **task 工具就是一把普通 @tool**：模型调用它，和调用计算器没有本质区别；区别在于函数体里起的不是算式，而是另一个 agent。
- **后台线程 + 轮询**：子代理跑在 `ThreadPoolExecutor` 的工作线程里；task 工具在原地轮询结果对象，直到终态（completed / failed / timed_out）才把结果作为工具返回值交差。
- **并发闸（BoundedSemaphore）**：一个只有 N 个名额的信号量。子代理起跑前先领名额，领不到就排队。
- **InjectedToolCallId**：v02 见过的注入参数——框架把本次 tool_call 的 id 塞进工具函数，学习版用它拼任务号，让输出可复现。
- **工具返回值就是 ToolMessage**：task 函数返回什么字符串，模型下一轮就"看到"什么。所以事件流水、结果、错误，全部要压进一个返回值里——这是子代理与主对话之间唯一的信使。
- **线程安全的终态**：一个任务的结果可能被两个线程写（执行线程与超时看门狗），谁先落笔谁说了算，后来的写不进去。这需要一把锁加一个"只认第一次"的判据。

## 原理：外包为什么必须带合同

把子任务交给另一个 agent，最天真的写法是"复制主 agent，再来一个"。这个写法有三宗罪。第一，**上下文爆炸**：子代理若继承主对话，它每一轮都要重读全部历史，外包反而更贵；第二，**权限失控**：子代理若继承全部工具，它里面藏着一个 task 工具，可以无限再外包——套娃一开，进程里全是互相委派却没人干活的 agent；第三，**资源失控**：没有并发闸的话，模型一时兴起发十个委派，就有十个模型调用同时烧钱。

本体的答案是一份"入职合同"加一套执行基础设施。合同（`SubagentConfig`）写死四件事：用哪个模型（通常换成便宜的）、能用哪几组工具（清单里永远没有 task）、最多转几轮、系统提示词。执行基础设施做三件事：**后台线程执行**——`execute_async` 把任务丢进线程池，结果写进一个线程安全的 `SubagentResult`；**事件回报**——子代理每一轮的动向（调了什么工具、交了什么终稿）记进任务自己的事件流水，随 ToolMessage 带回主对话，主 agent 对"外包过程"有一扇透明窗；**轮询到终态**——task 工具不抛异常、不阻塞事件循环，只老实轮询，超时也翻译成一个终态而不是崩溃。

```
 主 agent                    task 工具                  子 agent（后台线程）
    │  task(prompt,...)          │                            │
    ├───────────────────────────►│ 查注册表 → 合同             │
    │                            ├─ execute_async ───────────►│ create_agent(受限模型
    │                            │  （领并发名额，满则排队）      │   + 受限工具集）
    │                            │◄──── 事件: 起跑/调工具/交稿 ──┤ 跑 System+Human
    │                            │  轮询到终态（毫秒级学习版）    │  没有 task 工具
    │◄─ ToolMessage(事件流水+结果)┘   清理：终态才删             │  的自己套娃
```

还有一个设计决定值得停下来看：task 工具是**同步等到底**的。从主模型的视角看，它只是发起了一次"有点慢的工具调用"——不需要学习任何异步协议，不需要理解任务句柄，委派的心智成本被压到和按计算器一样。真正的异步发生在框架层：线程池、信号量、轮询循环，模型一概看不见。复杂度没有消失，只是从模型眼前搬到了管道里——这和 v05"安全从工具搬进中间件"是同一类搬运：**把机制藏进基础设施，把简单的接口留给智能本身**。

主对话里只多了两样东西：一条 task 调用和一条带回结果的 ToolMessage。子代理中间转了几轮、读了几份资料，全被压进事件流水——**外包买的是结果，不是过程的全过程回放**，这正是主对话上下文不被撑爆的关键。

## 代码精读

**① 合同与注册表**（`v06_subagents/subagents.py::REGISTRY`，节选）：

```python
REGISTRY: dict[str, SubagentConfig] = {
    "reader": SubagentConfig(name="reader", model="cheap",
        system_prompt="你是资料精读员：读原文、给要点，不写文件、不再委派。",
        groups=["read", "count"]),
    "writer": SubagentConfig(name="writer", model="cheap",
        system_prompt="你是报告撰写员：只根据给定素材落盘报告。", groups=["write"]),
}
```

最要紧的是装配口径：`resolve_tools(cfg)` 只从 `TOOL_GROUPS` 展开工具，`task` 压根不在这张表里——**禁止套娃不是运行时的检查，而是构造时的不可能**。运行时检查可以被绕过，构造时不存在无法绕过。

**② 终态仲裁**（`executor.py::SubagentResult.try_set_terminal`）：

```python
def try_set_terminal(self, status, *, result=None, error=None) -> bool:
    with self._lock:
        if self.status.is_terminal:
            return False          # 终态只认第一次写入
        self.status = status
        ...
        return True
```

最要紧的是"抢"这个字：执行线程与超时看门狗会往同一个 holder 上写终态，谁先写到算谁——本体 executor.py 里多处调用同一个方法，正是这套仲裁。学习版保留仲裁本身，把看门狗缩进 `wait_terminal` 的预算里。另一处要紧的在 `emit`：事件写进**本任务自己的**流水而不是全局 stdout，并行线程互不踩写，main 按消息顺序打印——这就是 --fake 输出能逐字节复现的原因。

**③ task 工具的委托闭环**（`task_tool.py::task`，骨架）：

```python
cfg = get_subagent_config(subagent_type)
if cfg is None:
    return f"未知子代理类型 {subagent_type!r}，可用：{', '.join(available_names())}。任务未执行。"
executor = SubagentExecutor(cfg, _model_factory)
task_id = f"task-{tool_call_id}"
executor.execute_async(prompt, task_id=task_id)
r = wait_terminal(task_id)
cleanup(task_id)                 # 终态才清理，本体同款
```

最要紧的是第一行的失败姿势：类型不认识时**不抛异常**，把可用类型清单回给模型。工具报错的目的是让模型自纠，不是让人加班。

## 跑起来

```bash
conda run -n deerflow_lab python v06_subagents/main.py --fake
```

预期输出（确定性，重复运行逐字节一致）：

```
[用户] 派 reader 分别精读 alpha 和 beta 各给一句要点；派 writer 把 gamma 的要点写成一句话报告落盘。做完后再派 intern 收个尾。

[HumanMessage] 派 reader 分别精读 alpha 和 beta 各给一句要点；派 writer 把 gamma 的要点写成一句话报告落盘。做完后再派 intern 收个尾。
[AIMessage] tool_calls=[('task', {'description': '精读 alpha', 'prompt': '精读 alpha 并给出一句话要点', 'subagent_type': 'reader'}), ('task', {'description': '精读 beta', 'prompt': '精读 beta 并给出一句话要点', 'subagent_type': 'reader'}), ('task', {'description': '撰写 gamma 报告', 'prompt': '依据 gamma 撰写一句话报告并落盘', 'subagent_type': 'writer'})]
[ToolMessage] [task-c1] reader completed（2 轮）
  · 起跑 model=cheap
  · 第1轮 调工具: read_notes({'note': 'alpha'})
  · 第2轮 交终稿: alpha 要点：ReAct 是 model 与 tool 的回合制乒乓。
结果: alpha 要点：ReAct 是 model 与 tool 的回合制乒乓。
[ToolMessage] [task-c2] reader completed（2 轮）
  · 起跑 model=cheap
  · 第1轮 调工具: read_notes({'note': 'beta'})
  · 第2轮 交终稿: beta 要点：上下文是稀缺资源，每 token 都要值回票价。
结果: beta 要点：上下文是稀缺资源，每 token 都要值回票价。
[ToolMessage] [task-c3] writer completed（2 轮）
  · 起跑 model=cheap
  · 第1轮 调工具: write_report({'text': '护栏决定 agent 失控时砸的是墙还是客户，必须先立规矩再谈自由。'})
  · 第2轮 交终稿: 报告已落盘 data/report.txt。
结果: 报告已落盘 data/report.txt。
[AIMessage] tool_calls=[('task', {'description': '让实习生收尾', 'prompt': '把结论整理一下', 'subagent_type': 'intern'})]
[ToolMessage] 未知子代理类型 'intern'，可用：reader, writer。任务未执行。
[AIMessage] 汇总：两份要点已带回，报告已落盘；intern 类型不存在，下次改用注册表里的类型。

并发闸观察（MAX_CONCURRENT_SUBAGENTS=2）:
  同时运行峰值 = 2，因名额已满排队次数 = 1

子代理注册表（合同里就没有 task 工具 —— 禁止套娃）:
  reader: model=cheap groups=['read', 'count'] tools=['read_notes', 'word_count']
  writer: model=cheap groups=['write'] tools=['write_report']

报告文件 data/report.txt: 护栏决定 agent 失控时砸的是墙还是客户，必须先立规矩再谈自由。
```

三处精读：主 agent 的**第一条** AIMessage 一次性发了 3 个 task 调用，ToolNode 并行执行，三个子代理同时涌向只有 2 个名额的闸门——所以末尾"峰值 = 2、排队 = 1"是并发闸存在的铁证；`intern` 一节演示了失败自纠的闭环；`data/report.txt` 是 writer 子代理写的，主 agent 从头到尾没碰过 `write_report`。

## Python 小课堂：信号量就是"号码牌桶"

```python
import threading, time

gate = threading.BoundedSemaphore(2)      # 只有 2 个名额
peak = 0; now = 0; lock = threading.Lock()

def worker(i):
    global peak, now
    gate.acquire()                        # 领不到牌就老实等
    with lock:
        now += 1; peak = max(peak, now)
    time.sleep(0.2)                       # 假装在干活
    with lock:
        now -= 1
    gate.release()                        # 还牌给下一个人

ts = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
[t.start() for t in ts]; [t.join() for t in ts]
print(peak)                               # 恒等于 2，永远不会是 3
```

`acquire` 阻塞等待、`release` 唤醒等待者——并发上限由数据结构保证，与线程调度无关。这就是本版 `executor.py` 里 `_gate` 的全部语义；Bounded 只是多管一件事：release 次数不得超过初始名额，防手滑。

## 与市面对比

| 做法 | 代表 | 与本版的差别 |
|---|---|---|
| 手拉手多 agent | LangGraph 手写 StateGraph | 委派的拓扑写死在图里，灵活但要预先设计 |
| 运行时动态委派 | Claude Code 的 Task、DeerFlow task 工具 | 委派由**模型**在对话中临场决定，拓扑无人预先画 |
| 进程级 agent 总线 | A2A / ACP 协议 | 跨进程跨机器，DeerFlow 的 invoke_acp_agent_tool 走这条路 |

DeerFlow 选中间那条：委派是模型的一个工具调用，代价最小；约束靠合同而非协议，落地最快。代价是没有跨进程能力——所有子代理与主 agent 同生共死在一个 Python 进程里，这对"分析型 agent"够用，对"分布式 agent 集群"不够，所以本体另留了 invoke_acp_agent_tool 这扇跨进程的门。

## 与本体差异（诚实声明）

- 本体 `MAX_CONCURRENT_SUBAGENTS = 3`（subagents/executor.py 约 L1118）且线程池 max_workers 双保险；学习版取 2，只用一把信号量——名额调小是为了让"排队"在 3 个任务时必然发生，真实可见。
- 本体轮询间隔以秒计、超时预算数百秒，轮询间隙把 task_started/running/completed 推成 SSE 事件给前端；学习版毫秒级轮询、事件攒进 SubagentResult 由 ToolMessage 一次性带回。
- 本体子代理还有 token 归集（token_collector，子代理花掉的钱回溯记到父 run 账上）、步骤事件（step_events）、鉴权与 trace 字段、状态合同（status_contract）；学习版全部砍掉——记账与追踪是 v17 的事。
- 本体 task 工具还支持 resume（用原 task_id 续跑被中断的子任务）与后台模式（委派后先干别的、稍后收账）；学习版只演示最短路径：委派→等待→收账。
- 本体内置类型是 general_purpose / bash_agent；reader / writer 两个类型与 latency_ms 字段为学习版自拟（latency_ms 只为让 --fake 演示出真实并发时序）。合同的四要素（受限模型、受限工具集、轮数上限、系统提示词）与本体 SubagentConfig 语义一致；"子代理无 task 工具"、"终态只认第一次写入"、"终态才清理"、"未知类型回清单不自爆"四条不变量与本体一致。

## 练习

把 `main.py` 里 `lead_script()` 的第一条 AIMessage 中 `subagent_type="writer"` 那个调用复制一份，改成第四个委派（id 用 `"c9"`，prompt 仍含 "gamma"）。跑 `--fake`：末尾两行变为"峰值 = 2，排队次数 = 2"——闸还是那道闸，队伍更长了一倍。（自证：`因名额已满排队次数` 从 1 变 2。）

## 下一步

子代理把活干完了，但每次重启对话，agent 还是不认识你。v07 补上两样人味：聊完自动起标题，以及跨会话也记得你说过什么——防抖抽取、旁路注入的长期记忆。
