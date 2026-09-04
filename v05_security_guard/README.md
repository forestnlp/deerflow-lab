# v05 · 安全护栏：保险丝从灯泡里搬进配电箱

> 靠自觉的防线，在下一个粗心的人面前等于没有。

## 前情提要

v04 的沙箱把 READ-BEFORE-WRITE 写进了 `write_file` 函数内部——检查与干活长在同一个身体里。这一版做一场手术：把安全检查从工具里剜出来，装进中间件管道。工具从此"变笨"（只剩干活的本事），护栏由框架统一安装、对所有工具一体生效。

## 前置技术

- **wrap_tool_call**：v02 学过的洋葱圈钩子，包住每一次工具调用。本版用它的杀手锏——不调 handler、直接伪造 ToolMessage 返回，即"一票否决"。
- **before_agent**：run 起点钩子，返回字典即写入图 state。适合"开工前先把目录、身份准备好"。
- **图 state 的自定义键**：`AgentState` 子类加 `thread_data`/`sandbox` 键，中间件之间靠它传身份，工具从 `runtime.state` 里认领——这是中间件与工具之间的"公文通道"。
- **configurable 身份通道**：`invoke(..., config={"configurable": {"thread_id": ..., "user_id": ...}})`，中间件里 `get_config()` 读取。本体同法（thread_id 在 context 或 configurable 二选一）。
- **checkpointer**：`MemorySaver` 让同一 thread 的多次 invoke 续写同一份历史——步骤 2 能否否决，取决于步骤 1、3 的历史还在不在。

## 原理：安全为什么必须长在管道里

v04 的写法有一个致命的前提：**每个工具都记得自己检查**。这个前提在三个方向上同时崩塌。第一，工具会增多——今天加一个 `append_file`，明天加一个 `patch_file`，只要有一个作者忘了抄那三行，闸门就漏了。第二，工具来自四面八方——本体的工具经配置反射加载，MCP 工具从别的进程里长出来（v16），你无法要求第三方作者有同样的纪律。第三，检查与业务耦合——改工具逻辑时最容易顺手删掉的就是"不相干的"安全检查。

Web 开发二十年前就交过这份作业：鉴权从不写在每个 handler 里，而是写在中间件里，请求想碰业务必须先过闸。本体的答案一模一样，且拆成三枚各司其职的中间件。ThreadData 管**身份与地基**：run 一开始按 (user, thread) 建好目录、登记进 state——工具永远假设有目录可用，不必自己处理"目录还没建"的混乱。Sandbox 管**认领**：把沙箱身份绑定到 thread，同 thread 复用不重领。ReadBeforeWrite 管**否决**：每次 `write_file`/`str_replace` 之前核对——文件存在吗？存在的话，历史里有没有对这个文件**当前版本**的读取记录？没有就伪造一条错误 ToolMessage 塞回去，工具压根不知道有人想写它。

"当前版本"是版本闸门与"读没读过"之争的全部精髓。只记"读过 notes.md"是不够的：读完之后文件又被改了（哪怕是你自己写的），旧读所见已非现状。本体的解法优雅：读标记 = 读那一刻全文的 sha256，盖在 read_file 的 ToolMessage 上；写入必然改变哈希，旧戳自动作废——**改一次，重读一次**，不需要任何显式的"撤销"动作。

```
            write_file(notes.md) 请求
                    │
        ReadBeforeWrite.wrap_tool_call
                    │
     文件存在? ──否──► 放行（新建不拦）
        │是
     最新读戳哈希 == 当前文件哈希?
        │是                │否
        ▼                  ▼
     handler(request)   伪造 ToolMessage "blocked"
     （工具真正执行）     （工具根本不知道发生过什么）
```

## 代码精读

**① 分配与认领**（`v05_security_guard/guard_middlewares.py::ThreadDataMiddleware.before_agent`，骨架）：

```python
configurable = get_config().get("configurable", {})
user_id, thread_id = configurable.get("user_id", "anon"), configurable.get("thread_id", "default")
sbx = self._provider.get(user_id, thread_id)     # 触发目录创建
return {"thread_data": {"user_id": user_id, "thread_id": thread_id, "workspace_path": sbx.root}}
```

最要紧的是**时序保证**：before_agent 先于一切模型/工具调用，所以下游工具可以放心假设目录存在。本体同款逻辑还带 lazy_init 开关（学习版取 eager 路径），并把身份同时盖进首条用户消息的时间戳——那是可观测性的事，学习版砍掉。

**② 一票否决**（`guard_middlewares.py::ReadBeforeWriteMiddleware.wrap_tool_call`，骨架）：

```python
if name in ("write_file", "str_replace"):
    blocked = self._check_gate(request, name, path)
    if blocked is not None:
        return blocked            # handler 不被调用 —— 工具没执行，世界没改变
    return handler(request)
if name == "read_file":
    result = handler(request)
    self._stamp_read_mark(request, result, path)   # 盖哈希戳
    return result
```

最要紧的是 `return blocked` 这一行的分量：被否决的调用得到一条 `status="error"` 的 ToolMessage，模型看到的是"工具返回了错误"，而不是"框架崩了"——否决要有力，还要有礼貌。这就是 v02 练习里 Veto 的完全体。

**③ 哈希即闸门**（`guard_middlewares.py`，三行核心）：

```python
if self._latest_mark(request.state, path) == self._hash(current):
    return None        # 模型见过的版本 == 现在要覆盖的版本 —— 放行
```

最要紧的是**戳随消息走**：标记存在 ToolMessage 的 `additional_kwargs` 里，摘要把这条消息截掉，标记随之消失，闸门自动落下——本体注释里把这列为设计不变量："闸门绝不可能在读过的内容已不在上下文时仍然放行"。写后不刷戳，则封死了"用自己的旧写入冒充新读取"的钻空路子。

## 跑起来

```bash
conda run -n deerflow_lab python v05_security_guard/main.py --fake
```

预期输出（确定性，一个字不差）：

```
  >> [ThreadData] 分配工作目录 user=alice thread=t-a
  >> [Sandbox] 认领沙箱 sandbox_id=alice/t-a
[步骤 1 · alice 新建 —— 不存在，放行]
  [用户] 把『H1 收入 42.1 亿』写入 /mnt/user-data/notes.md
  [最后一条工具结果] Wrote 12 chars to /mnt/user-data/notes.md
  >> [ThreadData] 分配工作目录 user=alice thread=t-a
  >> [ReadBeforeWrite] 否决未读先写: /mnt/user-data/notes.md
[步骤 2 · alice 未读先写 —— 否决]
  [用户] 把 notes.md 直接改成『全年收入 90 亿』，不用读。
  [最后一条工具结果] Error: write_file blocked — /mnt/user-data/notes.md already exists and y…
  >> [ThreadData] 分配工作目录 user=alice thread=t-a
[步骤 3 · alice 先读后写 —— 放行]
  [用户] 先读 notes.md，再改成『全年收入 90 亿』。
  [最后一条工具结果] Wrote 9 chars to /mnt/user-data/notes.md
  >> [ThreadData] 分配工作目录 user=alice thread=t-a
  >> [ReadBeforeWrite] 否决未读先写: /mnt/user-data/notes.md
[步骤 4 · alice 写完再改 —— 版本变了，又否决]
  [用户] 再把 notes.md 改成『全年收入 95 亿』。
  [最后一条工具结果] Error: write_file blocked — /mnt/user-data/notes.md already exists and y…
  >> [ThreadData] 分配工作目录 user=bob thread=t-b
  >> [Sandbox] 认领沙箱 sandbox_id=bob/t-b
[步骤 5 · bob 同路径新建 —— 各关各的笼子，放行]
  [用户] 在我的 /mnt/user-data/notes.md 写入『bob 的备忘』。
  [最后一条工具结果] Wrote 7 chars to /mnt/user-data/notes.md

沙箱仓库（按身份隔离，真实路径）:
  alice/t-a -> data/sandbox/users/alice/threads/t-a/user-data  files=['notes.md']
  bob/t-b -> data/sandbox/users/bob/threads/t-b/user-data  files=['notes.md']
```

三处精读：步骤 2 被拦后磁盘上 `notes.md` 仍是步骤 1 的内容（可开文件验证）；步骤 4 的否决最能说明"版本"而非"读过"——明明步骤 3 刚读过，但那次写入刷新了哈希，旧戳作废；步骤 5 的 bob 与 alice 同名文件互不相干，末尾两行沙箱清单就是身份隔离的收据。

## Python 小课堂：哈希值当"版本指纹"

版本闸门的全部机关，10 行内说得完：

```python
import hashlib

def h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

content = "H1 收入 42.1 亿"
stamp_at_read = h(content)          # 读的那一刻盖章
assert h(content) == stamp_at_read  # 没变 → 允许覆盖
content = "有人抢先改了"             # 任何写入…
assert h(content) != stamp_at_read  # 戳自动作废 → 必须先重读
```

sha256 碰撞极难，可把"两份内容是否逐字节相同"压缩成一次字符串比较。git 的对象库、npm 的完整性校验、本体的读标记，底层全是这一招。

## 与市面对比

| 做法 | 代表 | 与本版的差别 |
|---|---|---|
| 工具自查 | v04 / 多数教程实现 | 检查与业务同体，漏一个工具漏一个洞 |
| 提示词恳求 | system prompt 里写"请先读再写" | 模型心情好才遵守，无法取证也无法执法 |
| 框架闸门 | DeerFlow ReadBeforeWriteMiddleware | 在调用边界执法，对全部工具一体生效，还带版本指纹 |

## 与本体差异（诚实声明）

- 本体身份取自 runtime.context 或 configurable，并经 `get_effective_user_id()` 走多租户体系；学习版只认 configurable 里的 user_id/thread_id。
- 本体 content_reader 走沙箱抽象接口并有锁（per scope×path 串行化同轮并发写，`WeakValueDictionary` 锁表）；学习版单线程演示，去掉全部锁。
- 本体另有 fork-restored 沙箱的 Overwrite 拆包、after_agent 释放等生命周期簿记；学习版沙箱常驻到进程结束、重置时统一 clear。
- 学习版取 SandboxMiddleware 的 eager 认领路径（本体默认 lazy_init 走 wrap_tool_call 固化）；否决文案为学习版自拟、语义与本体 `_BLOCK_MESSAGE` 一致。三枚中间件的 hook 选择、状态键与"读戳随消息存亡、写不刷戳"两条不变量与本体一致。

## 练习

把 `main.py` 步骤 4 的剧本改成两条调用：先 `read_file` 再 `write_file`（内容"全年收入 95 亿"）。跑 `--fake`：步骤 4 的"否决"打印消失，最后一条工具结果变为 `Wrote 9 chars ...`——重读刷新哈希，闸门开。再改回原样确认可逆。（自证：步骤 4 的 `[最后一条工具结果]` 由 `Error: write_file blocked…` 变为 `Wrote 9 chars to /mnt/user-data/notes.md`。）

## 下一步

单兵作战的 agent 撑死了三头六臂。v06 请出 task 工具：大任务外包给子代理，还要管住并发与"子代理再开子代理"的套娃。
