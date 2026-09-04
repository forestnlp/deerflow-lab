# v10 · LLM 能力：会思考、能看图、按需取工具、先读说明书

> 给 agent 装上传感器，再把工具库的门锁换上按需的钥匙。

## 前情提要

v09 把行为失控全接住了，agent 已经打不死——但它还是个"单项选手"：不会思考（reasoning token 无处配置）、看不见图（消息只有纯文本）、工具全量裸奔（几十个 schema 每轮全绑给模型，费 token 还诱导误调）、上岗零培训（业务规范全靠用户口头交代）。这一版一次补齐四种"能力装备"：thinking 开关、多模态消息、工具分组延迟加载、技能注入。--fake 全程不真调 API，验证的是**配置读取与消息结构**——这两样恰恰是真实系统里最容易装错的部件。

## 前置技术

- **模型档案（config 的 models 段）**：v01 反射工厂的输入。本版给档案加了三张新字段：`supports_thinking`、`when_thinking_enabled`/`when_thinking_disabled`（外加简写 `thinking`），全部是"声明"，装配时合并成模型类的构造 kwargs。
- **content block**：消息的 content 除了字符串，还可以是块列表——`{"type":"text"}` 与 `{"type":"image", "source":{...}}` 混排。多模态的通用形状就是这一个列表。
- **工具 schema 绑定**：`create_agent` 把工具的 JSON schema 绑给模型，模型"看得见"哪些工具完全由这份清单决定。绑得越少，token 越省、误调越少。
- **frontmatter**：markdown 头部 `---` 包着的 YAML 元数据（name/description）。技能文件、提示词模板的通用打包法。
- **system prompt 拼装**：最朴素的注入点——把技能正文拼进 SystemMessage，模型开工前"读过说明书"。

## 原理：四件装备，一个共同敌人

共同敌人是**每轮模型调用都要付费的上下文**。thinking 参数错发给不支持的模型=整条请求 400；图片以裸 base64 字符串塞进 text=供应商视觉管道全对不上；工具全绑=每个 schema 每轮都收一遍租金；业务规范不注入=用户在对话里一遍遍口述。四块机制都朝这一个敌人开枪，但弹道各异。

**thinking** 的关键认知：它不是模型的布尔属性，而是**档案驱动的分支装配**。同一份模型档案里躺着两支 kwargs——开与关各走一支，装配器按运行时的 `thinking_enabled` 选支合并。本体的档案里还多一层：`thinking` 字段是 `when_thinking_enabled` 的简写，两者同时存在则合并（学习版照抄这条合并顺序：先 thinking 后显式支，后者覆盖前者）。为什么要 `supports_thinking` 守门？把 `thinking:{type:enabled}` 发给一个不认这个参数的端点，轻则忽略、重则报错——声明与校验都在配置层完成，代码层只管查表。

**多模态**的坑在"形状一致性"。图片从磁盘到供应商要走完这条链：路径 → 字节 → base64 → image block → 供应商适配器的视觉管道。任何一环把图片降级成文本（比如直接 `str(bytes)`），视觉模型就瞎了。所以 --fake 的验证策略是**结构自检**：块类型、media_type、base64 长度、source.type 四样对上，链条就算接通——真调 API 只是把最后一段透明管道走完而已。

**工具延迟加载**最值得琢磨的是"藏什么"。直觉是把未放行工具从 ToolNode 里整个拿掉，本体明确不这么干——注释原文："ToolNode still holds all tools (including deferred) for execution routing"。藏的是**模型绑定清单里的 schema**（wrap_model_call 过滤 request.tools），执行路由表保持全量。为什么？因为模型有时明知山有虎——硬调一个它记得名字的工具。此时 ToolNode 还认得路，中间件的 wrap_tool_call 闸门才能优雅拦下并回一句"先 load_tools 再试"；若 ToolNode 也不认得，模型收到的只是冷冰冰的 "not a valid tool"，学不会正确的路。看不见（防误调）与拦得住（教正确），是同一枚硬币的两面。

**技能注入**是四件里最"低技"的：读 md、剥 frontmatter、全文拼接。没有检索、没有按需激活——本体 skills 家族（catalog/parser/tool_policy/security_scanner 一大串）比这复杂一个数量级，但**地基就是拼接**：知识进不去上下文，一切花活免谈。学习版刻意停在地基，把"什么该拼进 system prompt、什么该做成工具"留给读者体会（答案在练习里埋了一半）。

顺带把"该不该拼"的判断尺说破：技能是**每次都要遵守的纪律**（语气、口径、格式），所以进 system prompt，常驻、无条件生效；工具是**需要时才用的能力**（查数、算式），所以进绑定清单，按需放行。同一份知识若只在特定任务用到，拼进 system prompt 就是给每一轮无差别付费——本体的 skill 目录化+按需加载就是这条尺度的工程化：先让模型读"技能清单的清单"（描述），真要用再加载正文。

```
 config.yaml                    main 装配                  每次模型调用
 ────────────────────────────────────────────────────────────────────
 models[thinking 三支] ──► model_kwargs(开关) ──► ChatModel kwargs
 uploads/chart.png   ──► image content block ──► HumanMessage[文本,图]
 data/skills/*.md    ──► compose_system_prompt ► SystemMessage(说明书)
 tools:[{name,group}]──► GROUPS 注册表 ──► wrap_model_call 滤 schema
                                   └──────► wrap_tool_call 拦硬调
```

## 代码精读

**① 档案选支**（`capabilities.py::model_kwargs`，节选）：

```python
archival = {"name", "display_name", "use", "supports_thinking",
            "when_thinking_enabled", "when_thinking_disabled", "thinking"}
merged = {k: v for k, v in entry.items() if k not in archival}
if thinking_enabled and not supports:
    raise ValueError(...)          # 声明与校验都在配置层
if thinking_enabled:
    merged.update(entry.get("thinking") or {})
    merged.update(entry.get("when_thinking_enabled") or {})
```

最要紧的是那个 `archival` 排除表：`supports_thinking` 这类字段是**给装配器看的说明书**，透传给 ChatOpenAI 构造函数就是未知参数报错。"哪些字段进模型、哪些只进装配逻辑"的边界，反射工厂时代就该划清。

**② 藏与拦的分工**（`deferred_tool_filter_middleware.py`，两头骨架）：

```python
def wrap_model_call(self, request, handler):
    kept = [t for t in request.tools if t.name in visible_tool_names()]
    return handler(request.override(tools=kept))      # 模型：看不见

def wrap_tool_call(self, request, handler):
    blocked = tool_registry.block_unpromoted(request)
    if blocked is not None:
        return blocked                                 # 硬调：拦得住
    return handler(request)
```

最要紧的是实跑输出里那两次 `calculator` 的不同死法：第一次被闸门拦下，回执写着"Call load_tools('math') first"（模型照做，第三轮清单里果然多了 calculator）；这正是本体 deferred 机制的教学切片——tool_search 版把"按组放行"换成了"按关键词检索提升"，骨架不变。

**③ 图片块的完整形状**（`capabilities.py::build_multimodal_message`）：

```python
return HumanMessage(content=[
    {"type": "text", "text": text},
    {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                 "data": data, "provider": {"file_provider": "file"}}},
])
```

最要紧的是 `source` 里 `provider.file_provider` 这个小尾巴：记录"这图是从本地文件来的"，供应商适配与审计（谁传的原图、要不要留档、超预算没）都靠它——本体 uploads/manager.py 的元数据思路，学习版留一个字段表意。另一处容易被略过却致命的细节是 `media_type` 的来源：这里按扩展名查表推断，生产环境应读文件头的 magic bytes——扩展名会骗人，`8950 4E47` 不会，本版主图的字节序列开头就是它。

## 跑起来

```bash
conda run -n deerflow_lab python v10_llm_capabilities/main.py --fake
```

预期输出（确定性，重复运行逐字节一致）：

```
config 声明的工具分组: {'default': ['query_sales', 'load_tools'], 'math': ['calculator'], 'word': ['word_count']}

====================================================
步骤 1 · thinking 开关（config 的三支字段 → 模型 kwargs）
====================================================
  thinking=False -> kwargs = {'model': 'gpt-4o-mini', 'base_url': '$DFL_BASE_URL', 'api_key': '$DFL_API_KEY', 'temperature': 0.2, 'extra_body': {'thinking': {'type': 'disabled'}}}
  thinking=True  -> kwargs = {'model': 'gpt-4o-mini', 'base_url': '$DFL_BASE_URL', 'api_key': '$DFL_API_KEY', 'temperature': 0.2, 'extra_body': {'thinking': {'type': 'enabled', 'budget_tokens': 2048}}}
  （在线路径这两套分别喂给 ChatOpenAI(...)；--fake 下只验配置读取）

====================================================
步骤 2 · 多模态消息结构（不真调 API，只验消息形状）
====================================================
  content blocks = ['text(这张趋势图里哪个月跌得最狠？)', 'image(image/png, 96B b64)']
  第2块 source.type = base64

====================================================
步骤 3 · 技能注入（frontmatter 解析 + 全文拼进 system prompt）
====================================================
  技能 报告语气: 经营报告的措辞与口径纪律。（正文 50 字）
  技能 表格优先: 呈现形式的选择规则。（正文 35 字）
  system prompt 总长 150 字符，含技能名: ['报告语气', '表格优先']

====================================================
步骤 4 · 工具分组延迟加载（default 先上岗，load_tools 再放行）
====================================================
  >> [DeferredTools] 本轮模型可见工具: ['load_tools', 'query_sales']
  >> [DeferredTools] 否决未放行工具: calculator
  >> [DeferredTools] 本轮模型可见工具: ['load_tools', 'query_sales']
  >> [DeferredTools] 本轮模型可见工具: ['calculator', 'load_tools', 'query_sales']
  >> [DeferredTools] 本轮模型可见工具: ['calculator', 'load_tools', 'query_sales']
  [SystemMessage] 你是邮政经营分析助手。

# 已装载技能
## 报告语气
经营报告的措辞与口…（150 字符）
  [HumanMessage] ['text(这张趋势图里哪个月跌得最狠？)', 'image(image/png, 96B b64)']
  [HumanMessage] 算 (2+3)*7，然后给结论。
  [AIMessage] tool_calls=['calculator']
  [ToolMessage] Error: Tool 'calculator' belongs to group 'math' which is not loaded yet. Call load_tools('math'
  [AIMessage] tool_calls=['load_tools']
  [ToolMessage] math 组已放行，新增可用：calculator。下次模型调用起生效。
  [AIMessage] tool_calls=['calculator']
  [ToolMessage] 35
  [AIMessage] 结论：(2+3)*7 = 35；先放行再调用，两步完成。

  各次模型调用看到的工具清单（schema 过滤的直接证据）:
    第1次: ['load_tools', 'query_sales']
    第2次: ['load_tools', 'query_sales']
    第3次: ['calculator', 'load_tools', 'query_sales']
    第4次: ['calculator', 'load_tools', 'query_sales']
  最终放行组: ['default', 'math']
  system prompt 落盘 data/system_prompt.txt（150 字符）
```

三处精读：步骤① 两行 kwargs 只差 `extra_body` 一支，thinking 开关的全部物理效应就这么多；步骤④ 的"各次可见清单"是渐进放行的直接证据——第 1、2 次没有 calculator（schema 被藏），第 3、4 次有了（load_tools 放行后自动进清单）；`word_count` 到终都没有出现（word 组没人放行），config 里声明它、data 里却无 trace，说明**声明≠上岗**。

## Python 小课堂：字符串三切口剥 frontmatter

```python
text = "---\nname: 报告语气\ndescription: 口径纪律。\n---\n结论先行，数字带单位。"

_, fm, body = text.split("---", 2)          # 第二次出现 --- 处切开，尾段留余量
print(fm.strip())                            # name: 报告语气
print(body.strip())                          # description 之后……不对！

# 稳健版：先按第一个换行确认，再限定切两次
first, rest = text[4:].split("\n---\n", 1)
print(first)          # name: 报告语气\ndescription: 口径纪律。
print(rest.strip())   # 结论先行，数字带单位。
```

`split(sep, maxsplit)` 的第三个返回值是"剩余全文"，正文里再出现 `---` 也不会被误切。YAML 解析交给 `yaml.safe_load`（学习版即如此），但**边界切分**这刀必须自己下准——本体 skills/parser.py 处理的 corner case（缺 frontmatter、BOM 头、CRLF）多得多，起手式都是这一刀。

## 与市面对比

| 做法 | 代表 | 与本版的差别 |
|---|---|---|
| thinking 硬编码 | 各家 SDK 的 `reasoning_effort` 参数 | 写死在调用处，换端点就 400 |
| 档案驱动 thinking | DeerFlow model_config 三支字段 | 开关/预算/端点差异全进配置 |
| 工具全绑 or 全不绑 | 多数 agent 框架 | 两头极端：token 浪费 or 能力缺失 |
| 藏 schema + 拦硬调 | DeerFlow deferred + tool_search | 渐进放行，模型自己学会"先检索再调用" |

Anthropic 的 tool use 文档同样建议少绑工具以降误调，但检索式提升（先搜索、后提升、再调用）是 DeerFlow 走得更远的一步——把"工具选择"本身做成了模型可学习的动作序列。

## 与本体差异（诚实声明）

- **主题聚合声明**：本体没有 `agents/llm_capabilities.py`（deerflow-lab 总 README 的版本地图该格写的即"主题聚合"名）；本版的四个零件分别对位 config/model_config.py（thinking）、uploads/+tools/builtins/view_image_tool.py（视觉）、agents/middlewares/deferred_tool_filter_middleware.py + tools/builtins/tool_search.py（延迟加载）、skills/parser.py+catalog.py+lead_agent/prompt.py（技能）。
- thinking：本体在 create_chat_model 里合并档案三支并透传 provider 方言（Claude 的 budget_tokens、o 系列的 reasoning_effort 等修补胶水）；学习版只做档案合并与 supports 校验，provider 胶水全砍。
- 多模态：本体经 uploads/manager.py 落盘归档、view_image 工具让模型"主动看图"（读文件再回注块）；学习版只有"装配期拼块"一条路，无主动看图。image block 里 provider 元数据只留一个字段表意。
- 延迟加载：本体提升靠 tool_search 按关键词检索（deferred 集合与目录哈希在构造期注入，per-thread 提升簿记进 state）；学习版按组粗粒度提升、进程级集合簿记。"schema 藏于 wrap_model_call、硬调拦于 wrap_tool_call、ToolNode 保持全量"三条与本体一致。
- 技能：本体技能是可执行包（frontmatter 带 allowed-tools 策略、安全扫描、slash 激活、按描述目录化按需加载正文）；学习版全文注入、无策略无扫描，data/skills/ 两份 md 为学习版自拟样例。
- --fake 全程未真调任何 API；`$DFL_BASE_URL` 在打印中原样出现即为"配置未展开占位"的实证（真实密钥只会在在线路径的 `_expand_env` 里出现）。

## 练习

把 `config.yaml` 里 `calculator` 的 `group: math` 改成 `group: default`，跑 `--fake`：`>> [DeferredTools] 否决未放行工具: calculator` 消失，第 1 次可见清单就有 calculator，剧本里的 `load_tools("math")` 变成一次空转（回执"没有 'math' 组。可用组：word。"）。再改回去确认可逆。（自证：`否决未放行工具` 行数从 1 变 0。已实测。）

## 下一步

单机八件能力齐了，可它还只是一个脚本。v11 起进入"环三·让它成服务"：RunManager 管生命周期、StreamBridge 把事件流出去，一条 SSE 长连接把 agent 从终端里捞进浏览器。
