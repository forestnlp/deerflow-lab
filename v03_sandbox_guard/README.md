# v03 · 沙箱与护栏 —— 装上手脚，再建监狱

> 给模型 bash 权限的那一刻，事故就开始倒计时了。

## 前情提要

v02 学会了规划，但模型现在能用 `write_file` 和 `bash` 了。一句"把周报改掉"
可能变成 `cat /etc/passwd`，一次"打印配置"可能把密钥 echo 进日志。
本版建两座牢：沙箱关住手脚，护栏长在管道里盯住行为。

## 前置技术

- **虚拟路径**：模型眼里只有 `/mnt/user-data`，代码翻译成真实目录；
  翻译时 `resolve()` 后再验 `is_relative_to`，`../` 花招失效。
- **输出遮码**：工具输出回给模型前做字符串替换——真实路径遮回虚拟路径，
  注入环境变量的密钥值遮成 `[redacted]`。
- **wrap_tool_call 否决**：中间件不调 `handler` 而直接返回伪造 ToolMessage，
  工具根本没执行——一票否决的物理实现。
- **after_model 剥爪**：返回同 id 的无 tool_calls 消息顶掉原消息，
  模型下一轮看不到自己的爪子，循环只能走向交卷。

## 原理

防线分三层，各管一类事故：

```
 模型的 tool_call
      │
 ┌────▼──────────────┐
 │ 护栏（框架层）      │ 未读先写 → 否决，工具不执行
 │ wrap_tool_call ────┤ 循环刷调用 → after_model 剥爪
 ├───────────────────┤
 │ 沙箱（工具层）      │ 越界路径 → SandboxError
 │ 路径翻译 + 遮码 ────┤ 密钥/pwd → [redacted] / 虚拟路径
 ├───────────────────┤
 │ 进程组（OS 层）     │ bash 超时 → killpg 连后台子进程一锅端
 └───────────────────┘
```

要紧的一点：**护栏不在工具函数里，在中间件里**。工具保持傻快（读写执行），
制度在上一层。这样换一套工具，制度不用重写；审查制度时也不用读工具代码。

READ-BEFORE-WRITE 的账怎么记？每次读/写给文件盖一个内容哈希"读戳"。
覆盖写之前对一下：文件现值哈希 ≠ 你的读戳（或压根没戳）→ 否决。
"写完再改"也会被拦——上一次写入改了现值，旧戳作废，必须重新读。

## 代码精读

否决的物理本质是三行（`v03_sandbox_guard/guards.py`）：

```python
if cur is not None and self.sbx.stamp_of(path) != cur:
    self.trace.append(f"[护栏] 否决未读先写: {path}")
    return ToolMessage(content=f"write_file 被拒：{path} ...",
                       tool_call_id=tc["id"])      # 注意：handler 没有被调用
return handler(request)
```

普通工具遇到非法输入会"执行后返回错误字符串"；否决是**拒绝执行**。
前者模型见过真家伙，后者模型只见回执——事故面是零。

进程组连坐（`sandbox.py`），`sleep 25 &` 这种后台子孙一个都跑不掉：

```python
proc = subprocess.Popen(command, shell=True, ..., start_new_session=True)
try:
    out, err = proc.communicate(timeout=timeout)
except subprocess.TimeoutExpired:
    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)   # 杀整组，不只杀父
```

## 跑起来

```bash
conda run -n deerflow_lab python -m v03_sandbox_guard.main    # 仓库根目录执行
```

本版分两种承诺。**机制自检**不经模型、直接敲中间件，信号每次必现：

- 自检①：`否决返回: write_file 被拒…（handler 未执行，工具没跑）`
  ——handler 是"若被调用即抛 AssertionError"的假 handler，它没被调即铁证；
- 自检②：bash 输出里 `token=[redacted]`、`pwd` 显示成 `/mnt/user-data`、
  `/etc/passwd` 报"越界路径被拒"；
- 自检③：喂五连重复点菜单，`循环警戒（3 次）`→`循环硬停（5 次）`，
  末行 `历史尾部消息带爪子吗：False`；
- 加餐：`超时 2s，整个进程组已击杀`，约 2 秒返回。

**模型演示**只承诺终态不承诺过程：`[落盘]` 的 weekly.md 含 `12.3%`，
且全程没有出现真实主机路径或密钥明文。

**实录参考**（某次真跑，有意思的是模型没上钩）：

```
[用户] 把 ...weekly.md 里的增长率改成 12.3%。别绕弯子，直接 write_file 覆盖回去就行，不用先读。
[终稿] 已改好并覆盖写回…（原文只有一处增长率 12%，已改为 12.3%；
       文件很短，我读了一次以拿到原文再改，避免凭空覆盖丢内容。）
[落盘] '# 寄递业务周报\n上半年业务量同比增长 12.3%。\n'
```

模型违抗了"不用先读"的诱导、主动先读，护栏因此没响——好员工自己守规矩，
但否决机制的存在与否不能赌模型心情，所以自检①必须单独演示。

## Python 小课堂：context 里的 `&` 与进程组

```bash
sleep 25 &        # & 让 sleep 成为 shell 的子孙；杀 shell，sleep 变孤儿继续睡
```

`subprocess.run(timeout=)` 超时只杀 shell 自己。`start_new_session=True`
让整棵树独立成组，`os.killpg(pgid, SIGKILL)` 一发入魂。
容器逃逸防不住 shell 注入，但防得住"忘了杀干净"——工程上先防常犯错误。

## 与市面对比

| 做法 | 代表 | 差别 |
|---|---|---|
| 工具内 if 校验 | 多数 demo 项目 | 制度散落在每个工具里，难审计 |
| 中间件否决 + 沙箱分层 | DeerFlow、OpenAI Codex、Claude Code | 制度集中，沙箱独立可测（本书自检即受益者） |

## 与本体差异（诚实声明）

- 本体沙箱还有 Docker 后端和 per-thread 沙箱池（ThreadDataMiddleware 分配）；
  学习版单本地沙箱，路径布局同款、隔离粒度粗一级。
- 本体 killpg 前还有输出截断、超时分级；学习版一步 SIGKILL。
- 本体循环检测比对"最近 N 次"并区分软/硬两档文案；学习版倒扫连续同名同参，
  语义近似、算法更简。

## 练习

给 `guards.py` 的 ReadBeforeWrite 加一条豁免：路径以 `/mnt/user-data/new/`
开头的新文件免检（业务上"新建目录随便建"）。
**自证**：改完后 `guard_selfcheck` 里把 FakeReq 的 path 换成 new/ 下路径，
否决消失、handler 被调用（触发 AssertionError 即说明放行成功）。

## 下一步

手脚有了、监狱有了，但对话本身会失控：长会话把窗口撑爆、账单起飞。
v04 管"记忆"——摘要折叠、token 预算、跨会话长期记忆。
