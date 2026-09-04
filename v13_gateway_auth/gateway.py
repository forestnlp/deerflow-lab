"""gateway —— FastAPI 最小网关：把 authz / csrf / ratelimit 三件套装到路由上。

参考本体：backend/app/gateway/app.py（中间件与路由装配）
        backend/app/gateway/routers/runs.py（runs 路由，401/403/429 各归其位）
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request

from authz import Principal, load_key_table, require
from csrf import CSRFMiddleware
from ratelimit import PerThreadBucketRegistry


def create_app(*, config: dict, audit_log: Path | None = None) -> FastAPI:
    """按 config.yaml 装配网关：keys 段建密钥表，ratelimit 段建令牌桶。"""
    load_key_table(config.get("keys", {}))
    rl = config.get("ratelimit", {})
    buckets = PerThreadBucketRegistry(
        capacity=float(rl.get("capacity", 3)),
        refill_rate=float(rl.get("refill_rate", 1.0)),
    )

    app = FastAPI(title="v13 mini gateway")
    app.add_middleware(CSRFMiddleware)   # 最外层：先验 CSRF，再谈身份

    @app.get("/api/ping")
    async def ping() -> dict:
        return {"ok": True}              # 顺带让中间件给新客户端发 csrf_token cookie

    def _audit(line: str) -> None:
        if audit_log is not None:        # 运行时产物只写本版 data/
            with audit_log.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    @app.post("/api/threads/{thread_id}/runs")
    async def create_run(thread_id: str,
                         principal: Principal = Depends(require("runs:create"))) -> dict:
        if not buckets.allow(thread_id):        # 限流按 thread 分桶（本体同款粒度）
            _audit(f"429 thread={thread_id} caller={principal.name}")
            raise HTTPException(status_code=429, detail=f"thread {thread_id} rate limited")
        _audit(f"201 caller={principal.name} role={principal.role} thread={thread_id}")
        # 学习版不起真 agent：run 受理即成功，v11 的 worker 在这里接上
        return {"run_id": uuid.uuid4().hex[:8], "thread_id": thread_id,
                "status": "queued", "caller": principal.name}

    @app.delete("/api/runs/{run_id}")
    async def cancel_run(run_id: str,
                         principal: Principal = Depends(require("runs:cancel"))) -> dict:
        _audit(f"cancel caller={principal.name} run={run_id}")
        return {"run_id": run_id, "status": "interrupted"}

    return app
