# v18 · 装机：一份配置拉起整套，一条命令跑通全链

> 部署的最高境界：运维以为自己在改 Excel。

## 前情提要

到 v17 为止，学习版攒下一整箱零件：模型工厂、中间件、事件账本、渠道。
但每一版都靠 `main.py` 里的 Python 代码手工接线——换模型改代码，加工具
改代码，接新中间件还是改代码。DeerFlow 本体早就把这层脏活交给了声明式
配置加反射装配。本版把"配置驱动"贯彻到底：config.yaml 声明 models、
tools、middlewares 三段，`dfl.py` 一条命令装出来跑起来，写错的配置在
启动期就被拒收。

## 前置技术

- **反射**：`importlib.import_module` 加 `getattr`，把字符串 `"module:attr"`
  变成活的类或函数。v01 学过它的婴儿形态。
- **声明式配置**：程序描述"要什么"（一段 YAML），命令式代码执行"怎么做"。
  改行为的人（运维/产品）不必懂怎么做。
- **依赖注入**：对象不自己造零件，等着别人把零件塞进来。反射装配是它最
  朴素的实现。
- **启动期校验 vs 运行期爆炸**：错误发现得越晚，排查成本越高。配置校验
  把"少写一个 use"从生产事故降级为一条编译期风格的报错。
- **子命令 CLI**：`git commit` / `git push` 式的 argparse subparsers，一个
  入口多个动词。
- **幂等启动**：同一份配置跑一百次，世界状态一样。本版体现为每次启动清空
  重写 `data/`，产物绝不跨运行累积。

config.yaml 里最耐人寻味的其实是 `use` 这个字段名。它没说 "class"，也没说
"factory"，因为装配器**不关心**你给的是类、工厂函数还是现成实例——它只
负责把它变成符合类型契约的对象。字段名故意取得含糊，是给扩展留的门：
将来接一个"从远程仓库拉插件"的加载器，配置格式一个字都不用变。


## 原理：字符串到对象的三段路

本体 `reflection/resolvers.py` 只有一个函数 `resolve_variable`，加上一份
类型契约，撑起了全仓库的 models、tools、channels、sandbox 装配。三段路是：

```
 config.yaml                assembly.py                    运行时
┌──────────────────┐   ┌─────────────────────────┐   ┌─────────────┐
│ models:          │   │ ① validate_config       │   │             │
│   use: "pkg:C"   │──►│    纯文本检查：段齐不齐、 │   │  create_    │
│ tools:           │   │    use 像不像 module:attr│   │  agent(     │
│   use: "mod:t"   │   │ ② resolve_variable      │──►│   model,    │
│ middlewares:     │   │    import_module+getattr │   │   tools,    │
│   use: "mod:M"   │   │ ③ 类型契约 isinstance    │   │   middleware│
└──────────────────┘   │    工具必须 BaseTool…    │   │  )          │
     错误在这层就被赶回 │ ④ 实例化（类/工厂二选一） │   └─────────────┘
                       └─────────────────────────┘
```

为什么校验要拆成 ① 和 ③ 两步、而且 ① 不许 import？因为 ① 便宜到可以每次
提交都跑（`dfl.py validate-config` 秒回），它的职责是拦住**写法错**——缺
字段、use 少了冒号、段整个丢了。这类错误根本不值得启动 Python 解释器去
发现。③ 则拦住**身份错**：use 指向的东西确实 import 成功了，但它是个字符串
不是 BaseTool——说明写配置的人对"这个字段该填什么"有误解。便宜检查在前，
昂贵检查在后，每一层只花自己该花的钱，这是所有校验管道的通用骨架。

再往深想一层：声明式配置真正的红利不在"少写代码"，在**改动的权限分离**。
代码进仓库要过评审、跑测试、走发布；配置在部署机上改一行、重启进程就生效。
当一个新渠道、新模型要在客户内马上线，而没有条件走你的发版流程时，
"给他一份 YAML"与"给他一份源码补丁"是两种量级的信任成本。DeerFlow 把
models/tools/channels/sandbox 全部做成可声明组件，本质是在回答一个组织
问题：想让不懂这套代码的人，安全地改动这套系统的哪几样东西。答案是
"能在 YAML 里表达的都不需要懂代码"，以及"YAML 里表达不了的（比如中间件
顺序）他们就不该动"。边界划在哪，反射装配的段落就切在哪。


实例化那一步藏着一个真实的取舍：本体只支持"类 + kwargs 构造"（`Tool(**cfg)`），
学习版多让了一步——use 指向工厂函数也行（`fake_model:make_script_model`），
因为 --fake 模式的剧本模型需要"按剧本名造模型"这种带逻辑的构造。多花十行
代码，换来"fake 与真实同一条装配路径"，值得。

最后交代一下本版没有的东西，免得配置越写越大胆：middlewares 段的条目
顺序**就是**链上顺序（列表序），但本版没有像本体那样用类型系统锁死
"Clarification 必须最后"这类顺序约束——两个演示中间件顺序无所谓，真上
生产请把顺序校验补进 validate_config（练习的延伸方向）。

## 代码精读

`v18_packaging/assembly.py` 的 `resolve_variable`，本体的镜像：

```python
def resolve_variable(path: str, expected_type=None):
    try:
        module_path, attr = path.rsplit(":", 1)
    except ValueError as err:
        raise ImportError(f"{path!r} 不像变量路径，正确样式: pkg.sub.mod:attr") from err
    try:
        module = import_module(module_path)
    except ImportError as err:
        root = module_path.split(".", 1)[0]
        hint = MODULE_TO_PACKAGE_HINTS.get(root, root.replace("_", "-"))
        raise ImportError(f"无法导入 {module_path}；试 pip install {hint}") from err
    obj = getattr(module, attr)
    if expected_type is not None and not isinstance(obj, expected_type):
        raise TypeError(f"{path} 解析出 {type(obj).__name__}，但期望 …")
    return obj
```

这里最要紧的不是 rsplit，是那个 `MODULE_TO_PACKAGE_HINTS`。报错的含金量在于
**下一步动作**："无法导入 langchain_openai" 是症状，"试 pip install
langchain-openai" 是处方。本体源码里维护着同样的映射表，因为"少装一个
provider 包"是社区用户撞上的第一个墙——把墙修成门，是平台方的良心。对比一下反例：同一个错误若等到 `getattr`
之后第 200 行才炸，报出来的是 `AttributeError: 'NoneType' has no ...`，
用户面对的是天书；在边界上把错误翻译成人话，是基础设施最便宜的体验升级。

`validate_config` 的拒绝逻辑（`v18_packaging/assembly.py`）：

```python
REQUIRED_FIELDS = {"models": ["name", "use", "model"],
                   "tools":  ["name", "use"],
                   "middlewares": ["name", "use"]}

for section, fields in REQUIRED_FIELDS.items():
    entries = cfg.get(section)
    if entries is None:
        problems.append(f"缺少 [{section}] 段"); continue
    for entry in entries:
        name = entry.get("name", "<无 name>")
        for f in fields:
            if f not in entry:
                problems.append(f"[{section}] 条目 {name!r} 缺字段 {f!r}")
```

这里最要紧的是 `problems` 是**列表**不是异常：一次跑完把所有毛病数完，
而不是碰到第一个就停。改配置的人最恨的不是报错，是"修一个、跑一次、又冒
一个"的挤牙膏式反馈。

`v18_packaging/dfl.py` 的 run 子命令把所有零件收拢成一条线：

```python
model_name = "fake" if args.fake else "demo"
model = build_model(cfg, model_name)          # models 段按 name 选装
tools = build_from_section(cfg, "tools")      # 逐个反射 + isinstance 验货
mws   = build_from_section(cfg, "middlewares")
print(f"装配完成: model={model_name} tools={[t.name for t in tools]} …")
agent = create_agent(model=model, tools=tools, middleware=mws)
```

这里最要紧的是 `--fake` 与在线走**完全相同**的三行装配——唯一的分叉是
`model_name` 选了哪一条配置。STYLE 规范"fake 与真实同一代码路径"到这一版
才算真正兑现：前面各版的 fake 分叉还藏在代码里，这一版藏在配置里。

## 跑起来

```bash
conda run -n deerflow_lab python v18_packaging/main.py --fake
conda run -n deerflow_lab python v18_packaging/dfl.py validate-config \
    --config v18_packaging/config_broken.yaml     # 故意写错，看它被拒
```

main.py 预期输出（`now` 工具返回的年月随运行当月变化，其余一字不差）：

```
【A】validate-config：错误配置活不过启动期
  config.yaml -> 通过
  config_broken.yaml 被拒绝: [models] 条目 'demo' 缺字段 'model'
  config_broken.yaml 被拒绝: [tools] 条目 'ghost_tool' 缺字段 'use'
  config_broken.yaml 被拒绝: [middlewares] 条目 'broken' 的 use='middlewares_demo EchoUsageMiddleware' 不是 module:attr

【B】反射装配：三段 use 字符串变成三个活对象集合
  model      = ScriptChatModel (name=fake)
  tools      = ['get_indicator', 'now', 'shout']
  middleware = ['EchoUsageMiddleware', 'StampMiddleware']

【C】装配产物直接开跑（和手写 create_agent 没有任何区别）
  [用户] 查一下收入指标，然后收尾。
  >> [dfl-v18] 中间件已在请求链上
  >> [usage] 第 1 次模型调用，tool_calls=1
  >> [usage] 第 2 次模型调用，tool_calls=1
  >> [usage] 第 3 次模型调用，tool_calls=0
  [HumanMessage] 查一下收入指标，然后收尾。
  [AIMessage] tool_calls=['get_indicator']
  [ToolMessage] H1 寄递收入 42.1 亿元
  [AIMessage] tool_calls=['now']
  [ToolMessage] 2026年09月
  [AIMessage] 汇总：H1 寄递收入 42.1 亿元，时间戳已确认。任务完成。

收尾：加一个工具 = 往 tools 段加两行；换一个模型 = 改一个 name。代码零改动。
```

CLI 三个子命令的行为同样可验证：`list-tools` 打印三行工具名与说明（退出码 0）；
`validate-config` 对坏配置打印三条拒绝理由并返回退出码 **1**（CI 可以直接
拿它当门禁）；`run "问题" --fake` 打印同一套装配清单与消息流（终稿相同），并把终稿落进
`data/last_answer.txt`。

装配的验收证据在【B】那三行：模型是 `ScriptChatModel` 实例、工具是三件
`BaseTool`、中间件是两个 `AgentMiddleware`——三段的类型契约全部兑现。而
【C】里两个中间件的日志（`[dfl-v18]` 一次、`[usage]` 三次）证明它们不是
装完就躺在列表里睡觉，是真的挂进了请求链。很多"配置驱动"翻车就翻在最后
一公里：对象造出来了，却忘了递给 `create_agent`——本版的装配函数把
"装完必交付"写死在同一个函数里，不给人犯忘的机会。

## Python 小课堂：argparse 子命令十行版

```python
import argparse
ap = argparse.ArgumentParser(prog="dfl")
sub = ap.add_subparsers(dest="cmd", required=True)   # required 防裸跑
p = sub.add_parser("run"); p.add_argument("question"); p.set_defaults(fn=do_run)
p = sub.add_parser("list-tools"); p.set_defaults(fn=do_list)
args = ap.parse_args()          # "dfl run 你好" -> Namespace(cmd='run', question='你好')
raise SystemExit(args.fn(args)) # set_defaults 把动词绑到函数，main 只剩分发
```

要点在 `set_defaults(fn=…)`：子命令与处理函数的绑定发生在**注册时**，
分发处不需要 if-elif 链。加第四个命令（比如 `dfl doctor`）只加一个
`add_parser`，不改任何既有分支——CLI 也会腐化，防它腐化的就是这一招。

## 与市面对比

| 方案 | 代表 | 差别 |
|---|---|---|
| 代码装配 | 早期 LangChain 教程 | 改一个模型名要改代码、重部署 |
| 框架级配置文件 | LangServe、本体 config.yaml | 声明 + 反射 + schema 校验（本体用 Pydantic 模型） |
| 依赖注入容器 | Spring、FastAPI Depends | 同样解耦，但概念重；agent 场景三段式 YAML 已够用 |

本体用 Pydantic 模型给 config.yaml 做 schema（类型、范围、默认值全声明），
学习版手写的 REQUIRED_FIELDS 只是它最小的影子——真实项目请直接抄本体。

## 与本体差异（诚实声明）

- 本体配置层是 `config/app_config.py` + `config/*_config.py` 约 40 个 Pydantic
  模型，学习版是三段 dict 加 20 行手写校验——只保"缺字段/坏写法/身份错"
  三类拦截，无数值范围、无必填默认值体系；
- 本体反射入口 `reflection/resolvers.py::resolve_variable` 还支持缺失依赖
  提示（学习版保留了三条包名映射做样子）；本体的实例化路径由各自的
  loader 决定（如 tools 走 `resolve_variable` 后取属性），学习版的
  "类/工厂二选一"是学习版自拟的简化；
- 本体 `client.py`（DeerFlowClient）装配的是完整 lead_agent 链（30+ 中间件），
  本版 middlewares 段只装配两个演示中间件；
- 本版 CLI 只有三个子命令；本体对应物是 gateway HTTP API + Makefile + uv，
  覆盖面完全不同量级；
- `$ENV` 占位展开学习版只处理标量字符串，本体的密钥体系（keychain、
  workspace 隔离）未涉及。

## 练习

往 `config.yaml` 的 tools 段追加一条

```yaml
  - name: shout
    use: "tools_business:shout"
```

（其实已在，请反向操作：把它注释掉，再在 middlewares 段把 tag 改成
`tag: 账单`）。自证：`dfl.py list-tools` 只剩两个工具；`run … --fake` 的
usage 日志变成 `[账单] 第 1 次模型调用…`。全程零 Python 代码改动——改不完
就把练习当真了。再加一问：为什么 `validate-config` 被拒时要返回退出码 1
而不是打印完照样 0？因为 CLI 的返回码是说给**机器**听的话——CI 门禁、
容器编排的健康检查、部署脚本的 `&&` 链，全都只认这个。人可以看红字，
机器只看数字。一个不会用退出码的 CLI，等于在自动化流水线里装哑巴。

顺带把 `config.yaml` 与 `config_broken.yaml` 对照读一遍：好配置里每条
tools 都是"名字 + 从哪来"两行，坏配置里 `ghost_tool` 只有名字没有出处——
这正是最常见的手滑现场。留一份坏配置在仓库里当"反面教材 + 测试固件"，
比写十句"注意格式"的注释都管用。

## 下一步

装机单齐了，但渠道只接过一家。生产现实是微信群、飞书、钉钉同时涌进来，
而你的 agent 只有一个。v19 把渠道抽象成 BaseChannel 三件套，让"再接一个
渠道"从复制粘贴降级为实现三个方法。
