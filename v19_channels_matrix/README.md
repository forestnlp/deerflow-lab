# v19 · 渠道矩阵：一个 agent，随便插

> 渠道不是八个类，是一个类的八种答案。

## 前情提要

v14 搭好了渠道总线：chat→thread 映射、消息去重、/命令，机制齐了——但整套
长在一个微信形状的壳里。加第二个渠道时你会发现没法复用，只能复制粘贴再
改。本版把渠道机制抽成 `BaseChannel` 三件套（connect / push / on_message），
然后当场接上三个渠道——FakeIM、控制台、Webhook——演示同一个 agent 同时
服役三入口而消息绝不串门，外加开工前的连通性自检。

## 前置技术

- **渠道（Channel）**：IM 平台与 agent 系统之间的适配器，负责"收消息变请求、
  回答案变推送"。每个平台一套 API，但职责惊人地一致。
- **抽象基类（ABC）**：Python 的 `abc` 模块，@abstractmethod 强制子类交卷，
  不写全就实例化报错——契约由解释器执法，不靠口头约定。
- **入站/出站**：inbound 是用户消息流进来，outbound 是答案流回去。渠道层
  全部设计都围绕这两个方向展开。
- **联合键会话映射**：`(渠道名, 会话 id)` 二元组决定一个 thread——单靠
  chat_id 会撞名，两个平台都有 "room-1"。
- **连通性自检**：上线前主动问每个渠道"现在能通吗"，而不是等用户发消息
  没回音才知道机器人死了。
- **毒丸收尾**：往队列里投一个约定的 `None`，消费循环见到它体面下班——
  比强杀任务干净，比无限空转礼貌。

三个渠道在本版各有明确分工：FakeIM 代表"有脚本可依赖"的测试渠道，
ConsoleChannel 代表"人眼就是终端"的调试渠道，WebhookChannel 代表
"平台推我接"的被动渠道——入站方向一主动一被动，正好检验抽象层能不能
同时装下长轮询和回调两种世界。


## 原理：三件套为什么恰好是三件

把本体八个渠道实现（wechat/feishu/dingtalk/slack/telegram/discord/wecom/
github）的共性剥到骨头，只剩三件事，每件对应一个方向的流量或一段生命周期：

```
                     ┌─────────── agent 系统 ───────────┐
                     │   ChannelManager（本版 manager.py）│
                     │   去重 → (channel,chat)→thread    │
                     │   → run → 按 channel 字段回门      │
                     └──────▲──────────────────┬────────┘
                   inbound  │                  │  outbound（按渠道名找门）
              bus.inbound.put│                  ▼ bus.publish_outbound
        ┌────────────────────┴── 渠道抽象 BaseChannel ──┴─────────────────┐
        │   connect()      生命周期：建联（登录/长轮询/验签准备）          │
        │   on_message()   入站三件套：原始事件 → Inbound → 投总线          │
        │   push()         出站：Outbound → 平台 API（send 的学习版名字）  │
        ├──────────┬──────────────────┬──────────────────────────────────┤
        │ FakeIM   │ ConsoleChannel   │ WebhookChannel（内存假 HTTP）     │
        │ 脚本群消息│ 打印到终端        │ POST 落 outbox，接口形状同 httpx  │
        └──────────┴──────────────────┴──────────────────────────────────┘
```

"三件套"的推导不是拍脑袋：任何渠道都要（一）在某时刻开始收消息——connect；
（二）收到消息后把它变成系统内部事件——on_message；（三）系统产出了回复，
送它回平台——push。本体 Channel ABC 的 `start/stop/send` 正是同一组事实的
另一种切分（本体的 stop 在学习版里靠毒丸消息收尾，见差异节）。

抽象层最大的收益在**路由层的无知**：`manager.py` 里没有一个 if 判断消息来自
哪个渠道。回复发回哪个渠道，靠的是入站消息自带的 `channel` 字段——路由信息
随数据走，不随代码走。于是"再接一个新渠道"的操作变成：继承 BaseChannel、
答完三道题、注册进 bus。路由层一行业务代码不动。这就是 v05 讲过的老规矩的
渠道版：**正确性靠数据结构保证，不靠程序员记性好**。联合键 `(channel,
chat_id)` 就是那条数据结构——演示里三个渠道的会话都叫 room-1，映射出来的
线程却是 `thr-fakeim-room-1 / thr-webhook-room-1 / thr-console-room-1`，
撞无可撞。顺带解释 thread 一词的双关：LangGraph 里 thread 是"一条
对话的状态线"（checkpoint 挂在它下面），IM 里 thread 是"话题串"。两个世界
的会话粒度不同——微信群没有话题串概念，一个群就是一个话题。联合键映射的
另一半职责就是把 IM 的会话粒度**翻译**成图的状态粒度：学习版一个 (channel,
chat_id) 开一条线；本体还多一层 /new 命令主动换线、按渠道覆写粒度（飞书
话题级、微信群级各有 run_policy）。粒度选择没有标准答案，但翻译层必须
存在，否则两个"room-1"共享同一份对话记忆，那是比串线更可怕的事故——
两个群的人隔空共享记忆还互相看不见。

连通性自检是运维视角的必需品。它的三段式——connect、push 一枚探针、按渠道
各自的证据判卷（`probe_sent()`）——把"配置错了"从上线两小时后用户投诉，
提前到部署脚本的退出码里。缺 token 的 webhook 在自检里报红，总好过在线上
沉默。

自检还有第二层价值：**把故障定位从玄学变成二分法**。线上"机器人不回话"
的投诉，排查动线本来是黑盒；有了三段式自检，运维按 connect、push、路由
逐段试：connect 过而 push 探针丢——平台侧 token/网络问题；push 过而探针
消息回了 inbound 却没答案——manager 或 agent 侧问题。每次自检都在给故障
空间做二分，代价是几十行代码。顺带一提，自检消息一律用 `__probe__` 这类
保留 chat_id，各渠道判卷看它、日志过滤也看它，绝不让探针真的打扰真人——
体检用的试纸不该混进病人的输液管。

## 代码精读

`v19_channels_matrix/channels.py` 的抽象基类，全部法定义务就九个动词：

```python
class BaseChannel(ABC):
    name: str = "base"

    @abstractmethod
    async def connect(self) -> None: ...
    @abstractmethod
    async def push(self, msg: Outbound) -> None: ...

    async def on_message(self, inbound: Inbound) -> None:
        # 入站的默认动作是投总线。子类一般不用改——这正是抽象的意义。
        await self.bus.inbound.put(inbound)

    async def self_test(self) -> tuple[bool, str]:
        try:
            if not self.connected:
                await self.connect()
            await self.push(Outbound(self.name, "__probe__", "__ping__"))
            return (self.probe_sent(), "connect 成功，push 探针已送达")
        except Exception as exc:
            return (False, f"失败: {exc}")
```

这里最要紧的是 `on_message` 给了**默认实现**而 connect/push 不给。入站动作
在渠道之间几乎一样（变成 Inbound 投总线），是公共资产；建联与出站是各平台
的方言，必须自己写。抽象类的分寸就在这一线：能共用的坚决共用，是方言的
坚决不代写。

WebhookChannel 展示了一个诚实的假 HTTP——形状对了，将来换真的只动一行：

```python
class WebhookChannel(BaseChannel):
    name = "webhook"

    async def connect(self) -> None:
        if not self.token:
            raise RuntimeError("webhook 渠道缺 token，无法验证回调签名")
        self.connected = True

    async def handle_http_post(self, payload: dict) -> None:
        # 真实版：FastAPI 路由收到 POST 后调这里
        await self.on_message(Inbound(self.name, payload["chat_id"],
                                      payload.get("user", "anon"), payload["text"]))

    async def push(self, msg: Outbound) -> None:
        # 真实版：await httpx.post(self.endpoint, json=…, headers=auth)
        self.outbox.append({"endpoint": self.endpoint,
                            "chat_id": msg.chat_id, "text": msg.text})
```

这里最要紧的是 `connect` 里那行 raise：token 校验发生在**建联时**而不是
第一条消息进来时。自检之所以能当场揪出坏配置，是因为渠道作者把"我能工作
吗"这个问题的答案写进了 connect，自检只是把问题问出来。

`manager.py` 的路由核心薄得吓人，但这正是厚度该待的地方：

```python
async def dispatch(self) -> None:
    msg = await self.bus.inbound.get()
    if msg is None:
        return False                       # 毒丸：收尾
    if msg.message_id in self._seen:
        return True                        # 去重：长轮询/重推保护
    self._seen.add(msg.message_id)
    thread_id = self.thread_of(msg.channel, msg.chat_id)   # 联合键
    answer = await self.run_handler(thread_id, msg)
    await self.bus.publish_outbound(Outbound(msg.channel, msg.chat_id, answer))
```

这里最要紧的是 `Outbound(msg.channel, …)`——出站渠道名**抄自**入站消息，
全程没有任何"当前渠道"之类的全局变量。三个渠道的消息同时在途时，答案
各回各家的保证不是调度技巧，是这条字段随波逐流。

## 跑起来

```bash
conda run -n deerflow_lab python v19_channels_matrix/main.py --fake
```

预期输出（`[console->屏幕]` 行在【B】段内出现；其余确定）：

```
【A】连通性自检（对应本体渠道连接检查：connect + push 探针）
  [fakeim  ] OK   connect 成功，push 探针已送达
  [console ] OK   connect 成功，push 探针已送达
  [webhook ] OK   connect 成功，push 探针已送达
  [webhook(坏)] FAIL 失败: webhook 渠道缺 token，无法验证回调签名

【B】一个 agent 同时接三渠道（消息并发进入，回复各回各家）
  >> [fakeim<-张三]      收入多少？
  >> [webhook POST<-李四] 时长多少？
  >> [console<-王五]      再说一遍收入
  << [console->屏幕] Console 答案：H1 寄递收入 42.1 亿元。
  << [fakeim->room-1] FakeIM 答案：H1 寄递收入 42.1 亿元。
  << [webhook POST->https://fake.internal/hook] Webhook 答案：平均投递时长 26.4 小时。

【C】对账：路由审计 + 会话隔离
  回复 -> 渠道 fakeim   线程 thr-fakeim-room-1
  回复 -> 渠道 webhook  线程 thr-webhook-room-1
  回复 -> 渠道 console  线程 thr-console-room-1
  会话映射（联合键，chat_id 同名也不串）: {"fakeim:room-1": "thr-fakeim-room-1", "webhook:room-1": "thr-webhook-room-1", "console:room-1": "thr-console-room-1"}
  （审计已写 data/routing.json）
```

【B】里有个值得盯住的现象：三条消息按 fakeim→webhook→console 进来，console
的回复却**先**打印——因为 dispatch 按入队顺序处理，console 那条恰好剧本
无工具一步出答案。顺序会骗人，【C】的审计表不会：routed 三条的渠道与线程
一一对号。验证"不串"要看账本，不要看直播。

## Python 小课堂：ABC  enforcement 现场

```python
from abc import ABC, abstractmethod

class Channel(ABC):
    @abstractmethod
    async def connect(self): ...
    @abstractmethod
    async def push(self, msg): ...

class Lazy(Channel):
    async def connect(self): pass
    # push 忘了写

Lazy()        # TypeError: Can't instantiate abstract class Lazy
              #    with abstract method push
```

要点：抽象方法在**实例化时**执法，不是导入时。这比"文档里写一句子类必须
实现 push"强硬得多——本体 `app/channels/base.py` 的 Channel 也是 ABC，
八个渠道类谁没交卷，进程启动就地伏法，根本轮不到线上见。

最后看一眼【C】的数据形状：`threads` 是"联合键 → 线程 id"的字典，
`routed` 是"(渠道, 线程)"的时间序列。前者证明隔离成立，后者证明路由正确，
两份数据都落进了 `data/routing.json`。把路由决策落盘不是多余动作——当有人
问"张三在飞书群里 @ 的那句为什么答到了钉钉"，你需要能重播每一次路由决策。
可观测的老规矩（v17 讲的）在渠道层同样成立：**能被审计的行为才叫被控制的
行为**。

## 与市面对比

| 方案 | 代表 | 差别 |
|---|---|---|
| 每个平台一个入口 | 早期 bot 教程 | 逻辑八份，改一处 bug 修八遍 |
| 平台自己的 bot 框架 | 飞书/钉钉 SDK | 只管收发，会话映射/去重/agent 接线仍要自己做 |
| 统一渠道抽象 + 总线 | DeerFlow、LangChain Hub 类集成 | 新渠道三件套，路由/会话/去重全复用 |

Line Bot、Slack Bolt 这类官方 SDK 其实也有类似的 handlers 抽象，DeerFlow
多走的一步是所有渠道共享**同一条** thread 映射与同一个 agent——跨渠道的
会话统一（同一个人在两个渠道说话接得上）从这里起步。

## 与本体差异（诚实声明）

- 本体 `app/channels/base.py::Channel` 的三件套是 `start/stop/send`（+
  `send_file`/`_send_with_retry` 等出站治理），学习版改名成
  connect/push/on_message 并把"投总线"上浮为基类默认方法——语义对应，
  切分角度是自拟的；
- 学习版无 stop()，以毒丸消息（inbound 队列收到 None）结束消费循环；本体
  有显式优雅停机与出站重试（指数退避、失败落 `persistence/webhook_delivery`）；
- 本体的渠道连接检查散在各渠道 start 与 connection 绑定流程里
  （`connection_identity.py`、connect code 机制）；仓库中**没有**字面名为
  `connection_test` 的模块（已检索核实），本版的 self_test/probe_sent
  三段式自检为**学习版自拟**，只借用"渠道连接需要主动验证"这一本体思想；
- 学习版无 /命令（/new /status /help）、无去重持久化（重启后 `_seen` 清零）、
  无文件收发、无流式出站分片、无按渠道 run_policy（本体 `run_policy.py`、
  `feishu_run_policy.py` 等）；
- Webhook 的"HTTP"是内存 list；真实签名验证（时间戳+HMAC）一行都没有——
  接口形状对了，安全没抄。

## 练习

实现第四个渠道 `EmailChannel`（name="email"，push 把 `(chat_id, text)` 存进
`self.sent_mail`，connect 要求 config 里有 `smtp_host` 否则 raise），注册进
main.py 的【A】自检与【B】路由。自证：自检段出现 `[email(坏)] FAIL 失败:
缺 smtp_host` 一行；给它 inject 一条消息后，routed 审计出现第 4 行。

## 下一步
零件全了：循环、中间件、渠道、配置、观测。v20 总装——按本体
`build_middlewares` 的原始顺序把九环中间件一次插齐，CLI、调度器、渠道三个
入口汇进同一张图，交出一台五脏俱全的迷你 DeerFlow。
