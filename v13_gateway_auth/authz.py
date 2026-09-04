"""authz —— API key 鉴权依赖 + 简单 RBAC（admin/user 两级）。

参考本体：backend/app/gateway/authz.py（Permissions 常量、AuthContext、
        require_permission("runs", "create") 的 resource:action 模型）
        backend/app/gateway/deps.py（FastAPI 依赖注入入口）
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request


@dataclass(frozen=True)
class Principal:
    """一个 API key 背后的身份：谁是它、能干什么。"""

    name: str
    role: str          # "admin" | "user"
    permissions: frozenset[str]


# 与本体 Permissions 常量同款命名（resource:action）
USER = Principal("alice", "user", frozenset({"runs:create", "runs:read"}))
ADMIN = Principal("root", "admin", frozenset({"runs:create", "runs:read", "runs:cancel", "keys:manage"}))

# 本体密钥在 auth 体系/配置里；学习版放 config.yaml 的 keys 段（值是环境变量名）
KEY_TABLE: dict[str, Principal] = {}


def load_key_table(keys: dict[str, str]) -> None:
    """config.yaml 的 keys 段：key 名 -> 环境变量名；值形如 "admin:sk-xxx"。"""
    import os

    KEY_TABLE.clear()
    for role_and_key in keys.values():
        resolved = os.environ.get(role_and_key, "")
        if not resolved:
            continue                        # --fake 模式不要求真实环境变量
        role, _, key = resolved.partition(":")
        KEY_TABLE[key] = ADMIN if role == "admin" else USER


def authenticate(request: Request) -> Principal:
    """鉴权依赖：X-API-Key 头换 Principal。401/403 的分工见 README 原理。"""
    key = request.headers.get("X-API-Key")
    if not key or key not in KEY_TABLE:
        raise HTTPException(status_code=401, detail="missing or invalid API key")
    return KEY_TABLE[key]


def require(permission: str):
    """权限依赖工厂：@Depends(require("runs:create")) —— 本体同款 resource:action。"""

    def checker(principal: Principal = Depends(authenticate)) -> Principal:
        if permission not in principal.permissions:
            raise HTTPException(status_code=403, detail=f"role={principal.role} lacks {permission}")
        return principal

    return checker
