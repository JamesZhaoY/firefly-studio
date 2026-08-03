"""
Token Pool: 多账号连接池 + 自动负载均衡 + 失败冷却.

存储:
  data/firefly.db 的 accounts 表（token + cookies + 标签 + 元信息）

向后兼容:
  - 老的 data/current_token.json + data/storage.json 视为「隐式单账号」,
    当 data/accounts/ 目录为空时由 require_token() 直接读老路径, 零迁移成本。
  - 一旦上传任意账号, pool 接管, 老路径不再读 (避免双账号歧义)。

调用约定:
  pool = get_pool()
  account, release = pool.acquire()      # 阻塞直到有可用账号
  try:
      ... 用 account.token 做请求 ...
  finally:
      release(ok=True)                   # 或 release(ok=False, error="401")
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from db import Database


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


# 失败 → 冷却时间 (秒). ponytail: 数字拍出来够用, 精确阈值等跑出来再调。
COOLDOWN_AUTH = 60.0        # 401/403/quota/expired → 等 cookie / token 恢复
COOLDOWN_TRANSIENT = 10.0   # 408 / network → 短冷却
COOLDOWN_SERVER = 30.0      # 5xx → 中等冷却
COOLDOWN_HARD = 300.0       # 余额耗尽等明显「不可用」信号 → 长冷却

DB_PATH_ENV = "FIREFLY_DB_PATH"


def _default_db_path() -> Path:
    raw = env(DB_PATH_ENV)
    return Path(raw) if raw else Path(__file__).resolve().parent / "data" / "firefly.db"


# ── Account ────────────────────────────────────────────────────


@dataclass
class Account:
    id: str
    label: str
    client_id: str
    api_key: str
    token: str
    expires_at: float
    source: str              # "upload" | "ims_refresh" | "legacy"
    added_at: float
    cookies: list[dict[str, Any]] = field(default_factory=list)
    arp_session_id: str = ""
    org_id: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    disabled: bool = False   # 管理员手动停用

    # ── 健康度 (in-memory, 不落盘) ─────────────────────────────
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    last_error: str = ""
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    in_use: int = 0
    total_acquired: int = 0
    total_failed: int = 0
    total_succeeded: int = 0

    def is_available(self, now: float | None = None) -> bool:
        if self.disabled:
            return False
        if not self.token:
            return False
        if now is None:
            now = time.time()
        if self.expires_at and now >= self.expires_at:
            return False
        if now < self.cooldown_until:
            return False
        return True

    def public_dict(self) -> dict[str, Any]:
        """返回给前端的字段, 不含 token / cookies 等敏感数据."""
        now = time.time()
        cooldown_left = max(0.0, self.cooldown_until - now)
        return {
            "id": self.id,
            "label": self.label,
            "client_id": self.client_id,
            "source": self.source,
            "added_at": self.added_at,
            "expires_at": self.expires_at,
            "expired": bool(self.expires_at and now >= self.expires_at),
            "disabled": self.disabled,
            "healthy": self.consecutive_failures < 3 and cooldown_left <= 0 and not self.disabled,
            "cooldown_left_sec": round(cooldown_left),
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "in_use": self.in_use,
            "stats": {
                "acquired": self.total_acquired,
                "succeeded": self.total_succeeded,
                "failed": self.total_failed,
            },
            "has_cookies": bool(self.cookies),
            "token_preview": (self.token[:8] + "…") if self.token else "",
        }


# ── Pool ───────────────────────────────────────────────────────


class TokenPool:
    def __init__(self, database: Database | Path) -> None:
        # Path 仅保留给纯自检；运行时 get_pool() 始终传入 Database。
        self.db = database if isinstance(database, Database) else Database(database)
        self._lock = threading.RLock()
        self._accounts: dict[str, Account] = {}
        self._order: list[str] = []        # round-robin 顺序
        self._rr_cursor: int = 0
        self._cv = threading.Condition(self._lock)
        self._load()

    # ── 持久化 (SQLite) ──────────────────────────────────────

    def _load(self) -> None:
        for data in self.db.list_accounts():
            acct = self._from_dict(data)
            if acct:
                self._accounts[acct.id] = acct
        self._order = list(self._accounts.keys())

    def _persist(self, acct: Account) -> None:
        """写 SQLite; 不落 in-memory 健康度."""
        self.db.save_account(self._to_dict(acct))

    @staticmethod
    def _to_dict(a: Account) -> dict[str, Any]:
        return {
            "id": a.id,
            "label": a.label,
            "client_id": a.client_id,
            "api_key": a.api_key,
            "token": a.token,
            "expires_at": a.expires_at,
            "source": a.source,
            "added_at": a.added_at,
            "cookies": a.cookies,
            "arp_session_id": a.arp_session_id,
            "org_id": a.org_id,
            "extra_headers": a.extra_headers,
            "disabled": a.disabled,
        }

    @staticmethod
    def _from_dict(d: dict[str, Any]) -> Account | None:
        try:
            return Account(
                id=str(d["id"]),
                label=str(d.get("label") or d["id"])[:64],
                client_id=str(d.get("client_id") or ""),
                api_key=str(d.get("api_key") or d.get("client_id") or ""),
                token=str(d.get("token") or ""),
                expires_at=float(d.get("expires_at") or 0),
                source=str(d.get("source") or "upload"),
                added_at=float(d.get("added_at") or time.time()),
                cookies=list(d.get("cookies") or []),
                arp_session_id=str(d.get("arp_session_id") or ""),
                org_id=str(d.get("org_id") or ""),
                extra_headers=dict(d.get("extra_headers") or {}),
                disabled=bool(d.get("disabled")),
            )
        except Exception as e:
            print(f"[pool] 字段缺失 {d.get('id')}: {e}", flush=True)
            return None

    # ── 增删改 ────────────────────────────────────────────────

    def add(
        self,
        *,
        token: str,
        label: str,
        cookies: list[dict[str, Any]] | None = None,
        client_id: str = "",
        api_key: str = "",
        arp_session_id: str = "",
        org_id: str = "",
        extra_headers: dict[str, str] | None = None,
        expires_at: float = 0.0,
        source: str = "upload",
    ) -> Account:
        label = (label or "").strip() or f"account-{uuid.uuid4().hex[:6]}"
        with self._lock:
            # 标签去重
            base = label
            i = 2
            while any(a.label == label for a in self._accounts.values()):
                label = f"{base} ({i})"
                i += 1
            acct = Account(
                id=uuid.uuid4().hex[:12],
                label=label,
                client_id=client_id,
                api_key=api_key or client_id,
                token=token,
                expires_at=expires_at,
                source=source,
                added_at=time.time(),
                cookies=cookies or [],
                arp_session_id=arp_session_id,
                org_id=org_id,
                extra_headers=extra_headers or {},
            )
            self._accounts[acct.id] = acct
            self._order.append(acct.id)
            self._persist(acct)
            self._cv.notify_all()
            return acct

    def remove(self, account_id: str) -> bool:
        with self._lock:
            if account_id not in self._accounts:
                return False
            del self._accounts[account_id]
            self._order = [i for i in self._order if i != account_id]
            self.db.delete_account(account_id)
            self._cv.notify_all()
            return True

    def update(
        self,
        account_id: str,
        *,
        label: str | None = None,
        disabled: bool | None = None,
    ) -> Account | None:
        with self._lock:
            acct = self._accounts.get(account_id)
            if not acct:
                return None
            if label is not None:
                new = (label or "").strip() or acct.label
                if new != acct.label:
                    if any(a.label == new and a.id != account_id for a in self._accounts.values()):
                        raise ValueError(f"label '{new}' 已存在")
                    acct.label = new[:64]
            if disabled is not None:
                acct.disabled = bool(disabled)
                if not acct.disabled:
                    acct.cooldown_until = 0.0
                    acct.consecutive_failures = 0
            self._persist(acct)
            self._cv.notify_all()
            return acct

    def set_token(
        self,
        account_id: str,
        *,
        token: str,
        expires_at: float,
        client_id: str = "",
    ) -> Account | None:
        """IMS 刷新或手动换 token 时调用."""
        with self._lock:
            acct = self._accounts.get(account_id)
            if not acct:
                return None
            acct.token = token
            acct.expires_at = expires_at
            if client_id:
                acct.client_id = client_id
                acct.api_key = client_id
            self._persist(acct)
            self._cv.notify_all()
            return acct

    def should_auto_refresh(self, account: Account) -> bool:
        """只为过期、空 token 或明确鉴权失败的账号自动刷新。

        quota / rate-limit 冷却不能靠刷新 token 恢复；避免自动刷新把这些
        冷却状态提前清掉。
        """
        with self._lock:
            current = self._accounts.get(account.id)
            if not current or current.disabled or not current.cookies:
                return False
            now = time.time()
            if not current.token or (current.expires_at and now >= current.expires_at):
                return True
            if current.cooldown_until <= now:
                return False
            err = (current.last_error or "").lower()
            return any(
                marker in err
                for marker in ("401", "403", "unauthorized", "forbidden", "expired", "鉴权")
            )

    def mark_refreshed(self, account_id: str) -> Account | None:
        """记录一次明确成功的 token 刷新，并解除该账号冷却。"""
        with self._cv:
            acct = self._accounts.get(account_id)
            if not acct:
                return None
            acct.cooldown_until = 0.0
            acct.consecutive_failures = 0
            acct.last_error = ""
            self._cv.notify_all()
            return acct

    def mark_refresh_failure(
        self, account_id: str, *, seconds: float = COOLDOWN_AUTH, error: str = ""
    ) -> Account | None:
        """在线程安全地标记 IMS 刷新失败。"""
        with self._cv:
            acct = self._accounts.get(account_id)
            if not acct:
                return None
            acct.cooldown_until = max(acct.cooldown_until, time.time() + seconds)
            acct.last_error = (error or "IMS refresh failed")[:240]
            self._cv.notify_all()
            return acct

    def get(self, account_id: str) -> Account | None:
        with self._lock:
            return self._accounts.get(account_id)

    def list(self) -> list[Account]:
        with self._lock:
            return sorted(
                self._accounts.values(),
                key=lambda a: (a.disabled, a.label.lower()),
            )

    def status(self) -> dict[str, Any]:
        """聚合摘要给 /api/health 用."""
        with self._lock:
            accts = list(self._accounts.values())
            now = time.time()
            available = [a for a in accts if a.is_available(now)]
            return {
                "size": len(accts),
                "available": len(available),
                "disabled": sum(1 for a in accts if a.disabled),
                "cooling_down": sum(
                    1 for a in accts if not a.disabled and a.cooldown_until > now
                ),
                "expired": sum(
                    1 for a in accts if a.expires_at and now >= a.expires_at
                ),
                "strategy": "round_robin_with_cooldown",
            }

    # ── 核心: acquire / release ─────────────────────────────────

    def acquire(self, *, timeout: float = 30.0) -> tuple[Account, Callable[[bool, str], None]]:
        """阻塞直到拿到一个可用账号; 返回 (account, release_fn).

        release_fn(ok: bool, error: str = "") 由调用方在请求结束时调用, 用来:
          - ok=True  → 清空 cooldown / 累计成功
          - ok=False → 根据 error 关键字匹配冷却时间, 累计失败
        """
        deadline = time.time() + max(0.0, timeout)
        with self._cv:
            while True:
                now = time.time()
                idx = self._pick_available(now)
                if idx >= 0:
                    acct = self._accounts[self._order[idx]]
                    acct.in_use += 1
                    acct.total_acquired += 1
                    self._rr_cursor = (idx + 1) % max(len(self._order), 1)
                    return acct, _ReleaseFn(self, acct.id)
                if time.time() >= deadline:
                    raise RuntimeError(
                        f"token pool: 无可用账号 (size={len(self._accounts)}, "
                        f"all cooling down or disabled)"
                    )
                self._cv.wait(timeout=max(0.1, deadline - time.time()))

    def _pick_available(self, now: float) -> int:
        """从 rr_cursor 起找第一个可用账号的下标 (相对 _order). 找不到返回 -1."""
        n = len(self._order)
        if n == 0:
            return -1
        for step in range(n):
            i = (self._rr_cursor + step) % n
            a = self._accounts.get(self._order[i])
            if a and a.is_available(now):
                return i
        return -1

    def _release(self, account_id: str, ok: bool, error: str) -> None:
        with self._cv:
            acct = self._accounts.get(account_id)
            if not acct:
                return
            acct.in_use = max(0, acct.in_use - 1)
            if ok:
                acct.consecutive_failures = 0
                acct.cooldown_until = 0.0
                acct.last_success_at = time.time()
                acct.last_error = ""
                acct.total_succeeded += 1
                self._cv.notify_all()
                return
            acct.total_failed += 1
            acct.last_failure_at = time.time()
            acct.last_error = (error or "")[:240]
            acct.consecutive_failures += 1
            cooldown = _cooldown_for_error(error)
            # 连续失败叠加 (封顶 COOLDOWN_HARD * 3)
            if acct.consecutive_failures >= 2:
                cooldown = min(cooldown * (2 ** (acct.consecutive_failures - 1)), COOLDOWN_HARD * 3)
            acct.cooldown_until = time.time() + cooldown
            self._cv.notify_all()

    # ── 导入: 从 token.json + storage.json 创建账号 ────────────

    def add_from_files(
        self,
        *,
        token_payload: dict[str, Any],
        cookies: list[dict[str, Any]] | None,
        label: str,
    ) -> Account:
        """把 token_daemon 产出的 current_token.json + storage.json 合一份账号."""
        token = str(
            token_payload.get("token")
            or token_payload.get("access_token")
            or token_payload.get("value")
            or ""
        ).strip()
        if not token:
            raise ValueError("token 文件里找不到 token / access_token 字段")
        expires_at = float(token_payload.get("expires_at") or 0)
        if not expires_at:
            exp_in = int(token_payload.get("expires_in") or 0)
            if exp_in > 0:
                expires_at = time.time() + exp_in
        # 客户端 ID: token claims 优先, 再 fallback 到文件字段
        client_id = str(token_payload.get("client_id") or "")
        api_key = client_id
        # IMS cookie refresh 时记录的真实 api_key 也会写到 token.json 里
        if not api_key and cookies:
            api_key = ""
        arp = str(token_payload.get("arp_session_id") or "")
        if not arp and isinstance(token_payload.get("headers"), dict):
            arp = str(token_payload["headers"].get("x-arp-session-id") or "")
        extra_headers: dict[str, str] = {}
        if isinstance(token_payload.get("headers"), dict):
            for k, v in token_payload["headers"].items():
                if k.lower().startswith(("x-", "arp-")) and isinstance(v, str):
                    extra_headers[k] = v
        return self.add(
            token=token,
            label=label,
            cookies=cookies or [],
            client_id=client_id,
            api_key=api_key,
            arp_session_id=arp,
            expires_at=expires_at,
            extra_headers=extra_headers,
            source="upload",
        )


class _ReleaseFn:
    __slots__ = ("_pool", "_id", "_called")

    def __init__(self, pool: TokenPool, account_id: str) -> None:
        self._pool = pool
        self._id = account_id
        self._called = False

    def __call__(self, ok: bool, error: str = "") -> None:
        if self._called:
            return
        self._called = True
        self._pool._release(self._id, ok, error)


def _cooldown_for_error(err: str) -> float:
    """根据错误关键字匹配冷却时长."""
    e = (err or "").lower()
    if not e:
        return COOLDOWN_TRANSIENT
    # 余额 / 限流 / 鉴权 → 长冷却
    if any(k in e for k in ("quota", "credit", "exhaust", "taste_exhausted", "rate", "limit", "throttle")):
        return COOLDOWN_HARD
    if any(k in e for k in ("401", "403", "unauthorized", "forbidden", "expired", "鉴权")):
        return COOLDOWN_AUTH
    # 网络 / 超时 → 短冷却
    if any(k in e for k in ("408", "timeout", "timed out", "network", "connection")):
        return COOLDOWN_TRANSIENT
    if any(k in e for k in ("500", "502", "503", "504", "server")):
        return COOLDOWN_SERVER
    return COOLDOWN_TRANSIENT


# ── module-level singleton ─────────────────────────────────────

_POOL: TokenPool | None = None
_POOL_LOCK = threading.Lock()


def get_pool() -> TokenPool:
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = TokenPool(Database(_default_db_path()))
    return _POOL


def reset_pool_for_tests() -> None:
    """测试用: 重置 singleton."""
    global _POOL
    with _POOL_LOCK:
        _POOL = None
