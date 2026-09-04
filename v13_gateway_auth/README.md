# v13 · 大门：三道门互不越权

> 服务有了账本和直播，但大门连夜不闭户。

## 前情提要

v11 起了服务，v12 让它扛得住断电。但那个网关夜不闭户：任何脚本、任何人，
摸到端口就能起 run 烧你的 token，还能顺手 cancel 掉别人的 run。本版装上门卫，
四个最常见的门闩一次配齐：API key、CSRF、限流、RBAC。它们各管一段，谁也替代
不了谁——这是本版唯一但要记牢的主线。

## 前置技术

- **API key / Principal**：请求带 `X-API-Key` 头，网关查表换成一个 Principal
  对象（你是谁 + 你能干什么）。查不到就是 401——"我不认识你"。
- **CSRF（跨站请求伪造）**：恶意网页借你的浏览器、用你浏览器里存着的 cookie
  发出你以为没发过的请求。攻击者借的是**身份**，不是权限，所以防它得靠另一
  套机制，见原理。
- **双提交 cookie**：服务端把同一个随机 token 走两条通道发给客户端——一条塞
  进 cookie，一条给前端 JS 读；请求时必须两条都到且一致。
- **令牌桶**：桶里最多 n 个令牌，来一发扣一个，按速率回填。空桶 → 429。既允
  许短时突发，又掐得住持续刷屏。
- **RBAC**：Role-Based Access Control，本版两级角色 admin / user。权限名沿用
  本体的 `resource:action` 写法（`runs:create` / `runs:cancel`）。
- **HTTP 状态码分工**：401 是没认出你，403 是认出了你不许干，429 是你干得太
  快。三者语义不能串门，前端全靠它决定"该重新登录、该提示无权限、还是该退避
  重试"。

## 原理：三道门，四种伪装

四种攻击或事故，各骗不过哪道门，画开就清楚：

```
 请求 ─► [门1 CSRF] ─► [门2 鉴权] ─► [门3 RBAC] ─► [门4 令牌桶] ─► handler
          cookie==头?    key→Principal   权限表有吗    thread 桶有令牌吗
            │403           │401            │403          │429
            ▼              ▼               ▼             ▼
   骗法：网页借      骗法：瞎猜       骗法：user 的   骗法：合法请求
   cookie 发 POST    一个 key         合法 key 去     海淹一个会话
   破法：cookie 在  （不认识你       cancel          破法：按 thread
   头对不上→403      →401）          （角色不够       分桶，殃不到
                                     →403）          别的会话→429
```

双提交 cookie 的精妙在"两条通道"：服务端发一个随机 token，同时放进 **cookie**
（浏览器发请求时自动携带）和**响应体**（前端 JS 读出来、手动塞进
`X-CSRF-Token` 头）。跨站恶意页能让浏览器带上 cookie——那是浏览器几十年改不
掉的老毛病——却**读不到** cookie 的内容，于是伪造不了那个头。一票走自动通道，
一票走人工通道，两票不一致就是外人。

为什么要三道门而不是"一个 if 全搞定"？因为它们的判据来源不同：CSRF 看**请
求从哪来**（cookie 与头是否同源），鉴权看**你是谁**（key 换身份），RBAC 看**你
的角色允许吗**（权限表），限流看**流量形状**（时间窗内的速率）。判据不同，就
不可能用一个布尔表达式合并；强行合并的结果，通常是"登录后就一切放行"那种漏
洞。所以代码里 CSRF 是最外层中间件，鉴权和授权是 FastAPI 依赖，限流落在业务
入口，四层各有各的位置。

限流为何要 **per-thread 分桶**而不是全局一桶？全局桶下，一个疯刷的会话能把老
实会话全饿死，等于自己造了一次自我 DoS。DeerFlow 的会话以 thread 为单位，桶
就按 thread 发；一个会话刷屏，殃不到别的会话。还有一个数要交代：容量 3 不是
"每秒 3 次"，而是"最多攒 3 次突发"，长期平均速率由 `refill_rate`（每秒回填 1
个）决定。很多人把 capacity 当限流阈值调，结果系统闲了一会儿就能被连打三发
——那不是 bug，是令牌桶的本性：它买的是"允许突发"，代价就是瞬时可能超过心里
那个阈值。

## 代码精读

`v13_gateway_auth/authz.py` 的权限依赖工厂，本体同款 `resource:action`：

```python
def require(permission: str):
    def checker(principal: Principal = Depends(authenticate)) -> Principal:
        if permission not in principal.permissions:
            raise HTTPException(status_code=403, detail=f"role={principal.role} lacks {permission}")
        return principal
    return checker

# 路由上：Depends(require("runs:create"))
```

这里最要紧的是**嵌套依赖**：`require` 内部又依赖 `authenticate`，FastAPI 会先
解析出 Principal 再交给 checker。身份与授权因此拆成两个可复用零件——换鉴权方式
（比如换成 JWT）不动路由，换权限表不动鉴权。演示里 `Depends(require("runs:cancel"))`
就是 user 与 admin 分道扬镳的那一行。

`v13_gateway_auth/authz.py` 的鉴权依赖只有四行，但状态码选得很讲究：

```python
def authenticate(request: Request) -> Principal:
    key = request.headers.get("X-API-Key")
    if not key or key not in KEY_TABLE:
        raise HTTPException(status_code=401, detail="missing or invalid API key")
    return KEY_TABLE[key]
```

要紧的是"假 key 与无 key 同罪"：都回 401、detail 一模一样。若假 key 回 403、
无 key 回 401，等于告诉探测者"这个 key 存在但权限不够"，一个 key 枚举机就成型
了。安全里的规矩是：**能少说一个字就少说一个字**。

`v13_gateway_auth/csrf.py` 的判决只有三行：

```python
if request.method in _UNSAFE_METHODS:          # GET/HEAD/OPTIONS 豁免
    cookie = request.cookies.get(CSRF_COOKIE_NAME)
    if cookie and cookie != request.headers.get(CSRF_HEADER_NAME):
        return JSONResponse({"detail": "CSRF token missing or mismatched"}, status_code=403)
```

最要紧的是 `cookie and` 这三个字符：只有**已领过 cookie 的浏览器式客户端**才受
双提交约束。纯 API-key 脚本只发 POST、从不发 GET，永远不会被动领到 cookie，于
是不被卷进浏览器语义里——这正是演示里"无 cookie 的 401 归鉴权、有 cookie 的
403 归 CSRF"这种分工的实现。顺带说清 401 与 403 为什么不能互换：401 的语义是
"补上凭证再来"，客户端见到它该去重登；403 是"凭证没问题，此事不许"，重登一万
次也没用。把角色不足回成 401，前端就会陷入"反复登录还是失败"的死循环——状态
码不是给人看的表情，是给客户端下的指令。签发那半边同样克制：

```python
if request.method not in _UNSAFE_METHODS and CSRF_COOKIE_NAME not in request.cookies:
    response.set_cookie(CSRF_COOKIE_NAME, secrets.token_urlsafe(32), samesite="lax")
```

只在安全方法上发 cookie，等价于本体"登录流程首次签发"的位置；`secrets` 而非
`random`，因为这是安全随机数，用错库是那种"跑起来完全正常"的错。

`v13_gateway_auth/ratelimit.py` 的桶核心：

```python
def take(self, now: float) -> bool:
    self.tokens = min(self.capacity, self.tokens + (now - self.updated_at) * self.refill_rate)
    self.updated_at = now
    if self.tokens < 1:
        return False
    self.tokens -= 1
    return True
```

装配现场在 `gateway.py::create_app`，三件套的上车位置各有说道：

```python
load_key_table(config.get("keys", {}))
buckets = PerThreadBucketRegistry(capacity=float(rl.get("capacity", 3)),
                                  refill_rate=float(rl.get("refill_rate", 1.0)))
app.add_middleware(CSRFMiddleware)   # 最外层：先验 CSRF，再谈身份
```

要紧的是顺序即语义：CSRF 挂在中间件层，请求还没碰到路由就被过筛；鉴权/授权是
依赖，路由解析时才跑；令牌桶在 handler 体内最后看一眼。三件事若换个个儿——比
如把 CSRF 挪到鉴权后——攻击者就能用假 key 让请求死在 401，从状态码差异反推
CSRF 的存在与否。门的顺序也是防线的一部分。密钥装载同样有次序洁癖：config 里
只写**环境变量名**（`keys: {admin: DFL_ADMIN_KEY}`），值形如 `admin:sk-xxx` 从
环境读，密钥永不进文件，与 STYLE GUIDE 的密钥纪律对齐。

要紧的是**惰性回填**：没有后台线程定时往桶里加令牌，只在取用的那一刻按时间差
结算。没有请求时零开销，一万并发也不会多出一个月历般的定时器。演示里
capacity=3，同一瞬间连发 4 发，第四发的回填量约等于 0，于是 429。时间取自
`time.monotonic()` 而非 `time.time()`——挂钟可以被 NTP 往回拨，单调钟不会，限
流最怕的就是被时间倒流白送几个令牌。

## 跑起来

```bash
conda run -n deerflow_lab python v13_gateway_auth/main.py --fake
```

预期输出（本机实跑实录；csrf token 前缀与 run_id 每次随机，其余确定）：

```
[0] GET /api/ping -> 200，中间件签发 csrf_token cookie（llYCM5e-…）
[1] POST 无 API key          -> 401  missing or invalid API key
[2] POST 假 API key          -> 401  missing or invalid API key
[3] POST 有 cookie 缺 CSRF 头 -> 403  CSRF token missing or mismatched
[4] POST user key + CSRF 头  -> 200  {'run_id': 'e754911f', 'thread_id': 't-1', 'status': 'queued', 'caller': 'alice'}
[5] DELETE run（user 无 cancel 权限）-> 403  role=user lacks runs:cancel
[6] DELETE run（admin）      -> 200  {'run_id': 'r-42', 'status': 'interrupted'}
[7] 同 thread 连发 4 次（capacity=3）-> [200, 200, 200, 429]
[8] 换 thread 立刻放行（桶隔离）-> 200
[审计] data/audit.log 共 7 行，样例：201 caller=alice role=user thread=t-1
[断言] 401/403/429 各归其位：身份、CSRF/角色、流量三道门互不越权 —— 演示结束（退出码 0）
```

读这段输出要抓三对：[1][2] 同为 401（无 key 与假 key 同罪）；[3] 与 [5] 同为
403 但根因完全不同（前者 cookie 与头不匹配，后者 `role=user lacks runs:cancel`）；
[7] 与 [8] 同为 429 边界，换 thread 就放行，证明桶是按 thread 分开的。演示用
`TestClient`：真请求真响应真 cookie，但是进程内 ASGI，不占端口，跑完自然退出，
所以 `--fake` 不需要起服务也不需要第二终端。末尾那行审计样例也值得看一眼：
`201 caller=alice role=user thread=t-1`——身份、角色、对象三要素齐全，审计日志
的铁律是"三个月后凭这一行能还原谁干了什么"，而 429 那几行刻意只记 thread 与
caller、不刷全量请求体。

## Python 小课堂：闭包工厂——require 为什么返回函数

```python
def require(permission: str):                 # 外层：装配期执行一次
    def checker(user: str):                   # 内层：运行期每请求一次
        if permission not in PERMS[user]:
            raise PermissionError(f"{user} lacks {permission}")
    return checker
PERMS = {"alice": {"runs:create"}}
check = require("runs:create")
check("alice")          # 通过
# check("bob") -> PermissionError: bob lacks runs:create
```

`permission` 被内层函数"记"在闭包里，装配期一次、运行期无数次。装饰器、中间件、
依赖工厂全是一招变体。判断自己有没有写对，问一句就够：这个外层函数的参数，是
"配置"还是"数据"？是配置就该闭起来（`permission`），是数据就该走参数
（`user`）。这也解释了为什么 `require` 每次装配只调用一次、`checker` 每个请求
都要跑一遍：闭包把"配置成本"摊薄成一次，把"判定成本"留在运行期，是这类工厂
函数通用的分账方式。

## 与市面对比

| 方案 | 代表 | 与本版的差别 |
|---|---|---|
| Session + CSRF | Django / Rails | 本版主通道是 API key，CSRF 只护浏览器那条通道 |
| OAuth2 / JWT | LangGraph Platform Auth | 本体 `gateway/auth/` 亦有 jwt、oidc、session cookie；本版走最短路 |
| 网关层限流 | Nginx / Kong | 按 IP/路由限流；DeerFlow 按 thread 业务语义在应用内做 |
| 云端 API 网关 | AWS API Gateway | 密钥、配额托管；DeerFlow 自己管权限表与角色 |

## 与本体差异（诚实声明）

- 密钥体系：本体在 `gateway/auth/`（session cookie、OIDC、JWT、本地口令，见
  该目录 `jwt.py` / `oidc.py` / `session_cookie.py` / `local_provider.py`），
  本学习版只留 config.yaml 的 `keys:` 段 + 环境变量装载，config 里写的是变量名
  而不是密钥值；
- CSRF 严格度：本体 `gateway/csrf_middleware.py::CSRFMiddleware.dispatch` 对所有
  unsafe 方法强制双提交，cookie 或头**任一缺失即 403**，并用
  `secrets.compare_digest` 比较；本学习版放宽为"已持 cookie 才校验"、用 `!=`
  比较，放宽的理由见代码精读，比较函数是本版的偷懒；
- 授权实现：本体有 `@require_permission` 装饰器与可插拔 AuthorizationProvider
  （`gateway/authz.py`，约 L356；权限常量见同文件 `Permissions`），本学习版落在
  FastAPI 依赖上；本学习版 ADMIN 里的 `keys:manage` 为自拟权限名，本体
  `Permissions` 只有 threads、runs 两组共 6 个常量（`threads:read/write/
  delete` 与 `runs:create/read/cancel`）；
- 限流：本体的 429 是局部逐点做的（`gateway/routers/auth.py::_check_rate_limit`
  按 IP、`routers/browser.py`、`routers/channel_connections.py`），并没有全局令
  牌桶组件——**令牌桶是本学习版自拟**的标准算法；per-thread 这一粒度与本体
  "同线程单活跃 run"的准入粒度一致（v11 已见）；
- 本学习版 `create_run` 不起真 agent，受理即返回（v11 的 worker 在这里接上）；
  审计只写 `data/audit.log` 一行摘要，本体另有 trace 中间件与事件存储；模型权
  限（角色 × 可用模型）本学习版完全没做。

## 练习

把 `config.yaml` 的 `ratelimit.capacity` 改成 1、`refill_rate` 改成 100，重跑。
（本机实测答案：第 [7] 步变成 `[200, 429, 429, 429]`，随后那句
`codes == [200, 200, 200, 429]` 抛 AssertionError。为什么回填 100/秒却仍然全被
拒？因为回填是按时间差**结算**的：100/秒意味着攒满 1 个令牌要 10 毫秒，而这四次
POST 在同一个列表推导里连发，间隔远小于 10 毫秒，桶始终没攒够。也就是说
`refill_rate` 再高也救不了"瞬时连发"——想放行这四次，得让 capacity≥4，或者真的
sleep。这个反差正是惰性回填最反直觉的地方。）

## 下一步

大门装好了，可进出的只有 HTTP。老板的消息在微信里，不在 curl 里。v14 修一条
从 IM 到 run 的路。
