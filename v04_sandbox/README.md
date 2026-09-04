# v04 · 沙箱：给模型装上手脚，再连手脚一起关进笼子

> 权限给出去的那一刻，护栏就得先焊死。

## 前情提要

v03 让模型学会列计划，可计划写得再漂亮，模型依然手无缚鸡之力——查不了文件、跑不了命令。这一版给它装上手脚：读写文件、执行 bash。而工具一碰真实世界，安全问题就从"讲道理"变成了"上制度"。

## 前置技术

- **虚拟路径**：模型眼中的世界只有一个根 `/mnt/user-data`，框架负责翻译成磁盘上的真实目录。模型从不知道（也猜不到）真实落点。
- **进程组**：Unix 下 shell 起的子进程与自己同组；`start_new_session=True` 让整条命令树独立成组，`os.killpg` 一发 SIGKILL 全组连坐。
- **超时墙钟**：一条 bash 命令最多跑 N 秒（本体默认 600），到点必杀——否则一个前台 `sleep infinity` 就能挂死整个会话。
- **输出掩码**：工具结果回到模型之前做两道遮：真实主机路径反向遮回虚拟路径；注入给子进程的密钥值遮成 `[redacted]`。
- **READ-BEFORE-WRITE**：覆盖一个已存在的文件之前，必须先读过它。防的是"拿旧内容当真相对不起覆盖写"。本版把它写在工具里（自觉版），这正是下一版的存在理由。

## 原理：翻译官 + 笼子 + 遮羞布

沙箱这个词容易误导——`LocalSandbox` 其实没有虚拟机那堵墙，它的全部权力来自三件事：**路径翻译、进程管杀、输出过滤**。想明白为什么这三件够用，就明白了本地沙箱的设计。

路径翻译切断的是**信息面**：模型不知道真实目录长什么样，就写不出指向它的绝对路径；而 `_to_actual` 里 `normpath` 之后再验一次前缀，把 `../../etc/passwd` 这类逃逸在翻译层当场枪毙。注意顺序——先翻译再规范化，越界的定义权在框架手里，不在字符串中。

进程管杀切断的是**时间面**：`killpg` 而不是 `proc.kill()`。bash 起了 `sleep 25 &` 再自己等 `sleep 25`，超时若只杀 bash，那个后台 sleep 会活成孤儿继续吃资源。独立进程组 + 组杀，才是斩草除根。

输出过滤遮的是**回流面**：工具结果是模型下一轮的输入，是最容易泄密的单向通道。`pwd` 打印出真实路径？遮回去。脚本 debug 时 `echo $TOKEN`？遮成 `[redacted]`。有个反直觉的细节：长度不足 8 的密钥值**不遮**——两位地区码、四位年份在正常输出里满天飞，全遮的误伤比泄漏更贵。这是本体用注释专门论证过的取舍。

```
    模型视角            框架（本版代码）              真实磁盘
  /mnt/user-data/a.md  ─ _to_actual() 最长前缀+防逃逸 → data/sandbox/users/demo-user/
                                                    threads/thread-001/user-data/a.md
  bash "pwd"           ─ 翻译命令里的虚拟路径 → 进程组执行 → killpg 兜底
  输出回到模型          ← mask_local_paths（主机→虚拟） ← mask_secret_values
```

还有一层设计藏在工具签名里：每个工具的第一个参数都是 `description`，docstring 写明"Explain why you are running this command"。模型每次动爪子都必须先自述动机，这句话会进日志、进审计、进 v17 的可观测层。它不拦止任何操作，但让每次操作都自带证词——沙箱管住能力边界，说明书管住意图留痕，两者合起来才是可审计的手脚。

## 代码精读

**① 翻译与枪毙**（`v04_sandbox/sandbox.py::LocalSandbox._to_actual`，骨架）：

```python
rel = virtual_path[len(best_prefix):].lstrip("/")
full = os.path.normpath(os.path.join(best_actual, rel))
if full != best_actual and not full.startswith(best_actual + os.sep):
    raise ValueError(f"path traversal detected: {virtual_path!r}")
```

最要紧的是最后一行防逃逸：`..` 已被 normpath 展平，展平后还跑出沙箱根的就是越界。检查必须在规范化**之后**——`a/../../etc` 这种串，不展平根本看不出恶意。

**② 斩草除根**（`sandbox.py::execute_command`，骨架）：

```python
proc = subprocess.Popen(["/bin/bash", "-lc", command], ...,
                        start_new_session=True,        # 独立进程组
                        env={**os.environ, **self.secrets})
try:
    output, _ = proc.communicate(timeout=timeout)
except subprocess.TimeoutExpired:
    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)    # 整组连坐
```

最要紧的是 `start_new_session` 与 `killpg` 必须成对出现。只给独立身份不给全组处决，等于给罪犯发了户口本又不查户。密钥走 `env` 注入、绝不拼进命令串——命令串会进日志、进 trace、进模型历史。

**③ 自觉版 READ-BEFORE-WRITE**（`sandbox.py::write_file` 三行）：

```python
if Path(actual).exists() and not append and actual not in self.read_files:
    return ("Error: READ-BEFORE-WRITE — file exists but you have not read it "
            "this session. Call read_file first ...")
```

最要紧的恰恰是它的弱点：检查长在**工具里**。换一批工具、漏写一行、或者干脆有第二个写文件的入口，护栏就消失了。本体最终把它迁进 `read_before_write_middleware.py`，用 `wrap_tool_call` 在框架层强制——那是 v05 的全部剧情。

## 跑起来

```bash
conda run -n deerflow_lab python v04_sandbox/main.py --fake
```

预期输出（确定性，一个字不差；首行为本版目录内的相对路径，与运行位置无关）：

```
虚拟路径 /mnt/user-data  =  本版目录/{data/sandbox/users/demo-user/threads/thread-001/user-data}
[用户] 把『上半年寄递业务量增长 12%』写成周报文件，修订为 12.3%，用 bash 看字节数与目录，最后读 /etc/passwd 试试。

  [HumanMessage] 把『上半年寄递业务量增长 12%』写成周报文件，修订为 12.3%，用 bash 看字节数与目录，最后读 /etc/passwd 试试。
  [AIMessage] -> ['write_file']
  [ToolMessage] Wrote 24 chars to /mnt/user-data/outputs/weekly.md
  [AIMessage] -> ['write_file']
  [ToolMessage] Error: READ-BEFORE-WRITE — file exists but you have not read it this session. Call r…
  [AIMessage] -> ['read_file']
  [ToolMessage] # 寄递业务周报\n上半年业务量同比增长 12%。
  [AIMessage] -> ['str_replace']
  [ToolMessage] Edited /mnt/user-data/outputs/weekly.md
  [AIMessage] -> ['bash']
  [ToolMessage] [exit 0]\n60 outputs/weekly.md\n/mnt/user-data\n
  [AIMessage] -> ['bash']
  [ToolMessage] [exit 0]\ntoken=[redacted]\n
  [AIMessage] -> ['read_file']
  [ToolMessage] Error: ValueError: path outside sandbox: '/etc/passwd' (must start with /mnt/user-da…
  [AIMessage] 周报已生成并修订；密钥与主机路径已掩码，越界读取被沙箱拦截。

--- 加餐 · bash 超时与进程组击杀（不经过模型，直接敲沙箱） ---
  [killpg 演示] exit=124 超时通知=True 后台子进程连坐消失=True

产物落盘（真实路径，模型永远看不到这些）:
  users/demo-user/threads/thread-001/user-data/outputs/weekly.md
```

三处细读：`wc -c` 输出里 `pwd` 打印的**真实**工作目录已被遮回 `/mnt/user-data`；`echo token=$DFL_DEMO_TOKEN` 打出 `token=[redacted]`——密钥经 env 进了子进程，却回不了模型上下文；t2 被拒后模型的自救路径（读→改）恰好是制度期望的行为路线。

**④ 工具即接口**（`tools.py::build_tools`）：五个工具全部套 `guard` 壳，异常一律换成 `"Error: ..."` 字符串返回。最要紧的是异常去哪：抛出去，整轮 agent 崩溃，用户看到栈帧；收成返回值，模型看到错误说明，下一轮自己改正——输出里 t2 被拒后改走"读→改"就是这条设计的红利。工具边界是模型输入进入真实世界的国境线，海关的原则是：坏消息也要走正规通道。

## Python 小课堂：functools.wraps 与"套壳不丢魂"

五个工具都套了同一个异常兜底壳，靠的是装饰器工厂：

```python
import functools

def guard(fn):
    @functools.wraps(fn)            # 把 fn 的 __name__/__doc__ 等搬回壳上
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as exc:
            return f"Error: {type(exc).__name__}: {exc}"
    return wrapper
```

不写 `@functools.wraps`，`wrapper.__name__` 就叫 `wrapper`——`@tool` 会照着这个名字给模型注册一个名叫 wrapper 的工具，docstring 也一起丢。工具即接口，名字和说明书就是接口文档，套壳绝不能丢魂。

## 与市面对比

| 做法 | 代表 | 与本版的差别 |
|---|---|---|
| 真隔离 | Docker/Firecracker 容器沙箱（本体 AIO/E2B provider） | 隔离彻底但重；本体生产形态，学习版只留 Local 一条路 |
| 无沙箱 | 直接 subprocess 的玩具 agent | 一条 `rm -rf ~` 全剧终 |
| 路径翻译+进程治理 | DeerFlow LocalSandbox / 本版 | 零依赖可跑，安全靠翻译/击杀/过滤三道机制而非 OS 墙 |

## 与本体差异（诚实声明）

- 本体 LocalSandbox 还支持 Windows PowerShell/cmd、输出上限 10MB 的管道抽取、命令路径白名单式静态审查（`validate_local_bash_command_paths`）；学习版只走 POSIX bash，命令串原样执行。
- 本体路径翻译是双向多映射（自定义挂载、技能目录）；学习版只有一条 `/mnt/user-data`。
- 本体密钥来自请求级 secret 上下文（`read_active_secrets`），掩码长度阈值 8 与标记 `[redacted]` 同值同款；学习版密钥写死在演示脚本里。
- 本体 glob/grep 有 max_results 上限与中间截断；学习版 list_dir 只列文件不设上限（演示数据量小）。
- 工具签名保留 `description` 首参但无 runtime 注入（v05 补）；异常兜底在工具层，本体在 `ToolErrorHandlingMiddleware`。

## 练习

在 `sandbox.py::execute_command` 里把 `os.killpg(...)` 换成 `proc.kill()`，跑 `--fake`：`后台子进程连坐消失` 会变成 `False`（脚本里的 `sleep 25 &` 活了下来，可用 `ps` 亲眼确认后再 `kill` 掉）。改回 killpg 复跑恢复 True。（自证：同一命令两次运行的 `gone` 值不同。）

## 下一步

安全长在工具里，等于把保险丝焊死在每盏灯上。v05 把保险丝集中进配电箱：ThreadData 分配、wrap_tool_call 一票否决、READ-BEFORE-WRITE 中间件化——工具变笨，管道变强。
