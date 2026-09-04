"""v13 · 大门 —— 网关最小版：API key、CSRF 双提交、per-thread 令牌桶、简单 RBAC。

运行：
    conda run -n deerflow_lab python v13_gateway_auth/main.py --fake
    # 在线：export config.yaml keys 段声明的环境变量后去掉 --fake（长驻服务）

--fake 用 FastAPI TestClient（进程内 ASGI，真 HTTP 语义、零端口、自动关）：
    拿 csrf cookie → 纯脚本客户端（无 cookie）验证 401（无 key/假 key）
    → 浏览器式客户端有 cookie 但缺 CSRF 头 → 403 → 补齐头 200（user 起 run）
    → user 不能 cancel(403) 而 admin 能(200)
    → 同 thread 打满 3 个令牌第 4 发 429 → 换 thread 立刻放行（桶按 thread 隔离）
    → 断言审计日志 → 退出码 0。
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from gateway import create_app

DATA_DIR = Path(__file__).resolve().parent / "data"
CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

USER_KEY = "sk-user-demo"
ADMIN_KEY = "sk-admin-demo"


def demo_fake() -> None:
    shutil.rmtree(DATA_DIR, ignore_errors=True)   # 启动时清空，保证可重复运行
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    # --fake：把 config 里声明的两个演示 key 注入环境变量，走与在线同一条装载路径
    demo_values = {"admin": f"admin:{ADMIN_KEY}", "user": f"user:{USER_KEY}"}
    for key_name, env_name in config["keys"].items():
        os.environ[env_name] = demo_values[key_name]

    app = create_app(config=config, audit_log=DATA_DIR / "audit.log")
    client = TestClient(app)                      # 浏览器式：GET 一次领 csrf cookie

    r = client.get("/api/ping")
    token = client.cookies.get(CSRF_COOKIE_NAME)
    print(f"[0] GET /api/ping -> {r.status_code}，中间件签发 csrf_token cookie（{token[:8]}…）")

    script = TestClient(app)  # 纯 API-key 脚本：从没领过 cookie，CSRF 门不适用
    r = script.post("/api/threads/t-1/runs")
    print(f"[1] POST 无 API key          -> {r.status_code}  {r.json()['detail']}")
    assert r.status_code == 401

    r = script.post("/api/threads/t-1/runs", headers={"X-API-Key": "sk-wrong"})
    print(f"[2] POST 假 API key          -> {r.status_code}  {r.json()['detail']}")
    assert r.status_code == 401

    r = client.post("/api/threads/t-1/runs", headers={"X-API-Key": USER_KEY})
    print(f"[3] POST 有 cookie 缺 CSRF 头 -> {r.status_code}  {r.json()['detail']}")
    assert r.status_code == 403

    csrf_headers = {"X-API-Key": USER_KEY, CSRF_HEADER_NAME: token}
    r = client.post("/api/threads/t-1/runs", headers=csrf_headers)
    print(f"[4] POST user key + CSRF 头  -> {r.status_code}  {r.json()}")
    assert r.status_code == 200 and r.json()["caller"] == "alice"

    r = client.delete("/api/runs/r-42", headers=csrf_headers)
    print(f"[5] DELETE run（user 无 cancel 权限）-> {r.status_code}  {r.json()['detail']}")
    assert r.status_code == 403

    admin_headers = {"X-API-Key": ADMIN_KEY, CSRF_HEADER_NAME: token}
    r = client.delete("/api/runs/r-42", headers=admin_headers)
    print(f"[6] DELETE run（admin）      -> {r.status_code}  {r.json()}")
    assert r.status_code == 200

    codes = [client.post("/api/threads/t-burst/runs", headers=csrf_headers).status_code
             for _ in range(4)]
    print(f"[7] 同 thread 连发 4 次（capacity=3）-> {codes}")
    assert codes == [200, 200, 200, 429]

    r = client.post("/api/threads/t-other/runs", headers=csrf_headers)
    print(f"[8] 换 thread 立刻放行（桶隔离）-> {r.status_code}")
    assert r.status_code == 200

    audit_lines = (DATA_DIR / "audit.log").read_text(encoding="utf-8").splitlines()
    print(f"[审计] data/audit.log 共 {len(audit_lines)} 行，样例：{audit_lines[0]}")
    assert any(line.startswith("429") for line in audit_lines)
    print("[断言] 401/403/429 各归其位：身份、CSRF/角色、流量三道门互不越权 —— 演示结束（退出码 0）")


def online_serve() -> None:
    import uvicorn

    shutil.rmtree(DATA_DIR, ignore_errors=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    app = create_app(config=config, audit_log=DATA_DIR / "audit.log")
    print("在线模式：请先 export config.yaml keys 段声明的环境变量（值形如 user:sk-xxx）")
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("DFL_PORT", "8013")))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake", action="store_true", help="TestClient 离线演示，零端口零 API key")
    args = ap.parse_args()

    if args.fake:
        demo_fake()
    else:
        online_serve()


if __name__ == "__main__":
    main()
