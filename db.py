"""SQLite 任务与调用日志。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        with _lock:
            conn = self._conn()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL,
                        message TEXT DEFAULT '',
                        progress REAL DEFAULT 0,
                        prompt TEXT DEFAULT '',
                        model TEXT DEFAULT '',
                        model_version TEXT DEFAULT '',
                        params_json TEXT DEFAULT '{}',
                        result_json TEXT DEFAULT '',
                        outputs_json TEXT DEFAULT '[]',
                        error TEXT DEFAULT '',
                        traceback TEXT DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        finished_at REAL
                    );

                    CREATE TABLE IF NOT EXISTS api_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT,
                        phase TEXT DEFAULT '',
                        method TEXT DEFAULT '',
                        url TEXT DEFAULT '',
                        status_code INTEGER,
                        request_headers TEXT DEFAULT '',
                        request_body TEXT DEFAULT '',
                        response_headers TEXT DEFAULT '',
                        response_body TEXT DEFAULT '',
                        error TEXT DEFAULT '',
                        duration_ms REAL,
                        created_at REAL NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
                    CREATE INDEX IF NOT EXISTS idx_logs_job ON api_logs(job_id);
                    CREATE INDEX IF NOT EXISTS idx_logs_created ON api_logs(created_at DESC);

                    CREATE TABLE IF NOT EXISTS accounts (
                        id TEXT PRIMARY KEY,
                        label TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_label ON accounts(label);
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def list_accounts(self) -> list[dict[str, Any]]:
        with _lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT id, label, payload_json, created_at, updated_at FROM accounts ORDER BY label COLLATE NOCASE"
                ).fetchall()
            finally:
                conn.close()
        out = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
                if isinstance(payload, dict):
                    out.append(payload)
            except Exception:
                continue
        return out

    def save_account(self, account: dict[str, Any]) -> None:
        now = time.time()
        with _lock:
            conn = self._conn()
            try:
                conn.execute(
                    """
                    INSERT INTO accounts (id, label, payload_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        label=excluded.label,
                        payload_json=excluded.payload_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        account["id"],
                        account["label"],
                        json.dumps(account, ensure_ascii=False),
                        float(account.get("added_at") or now),
                        now,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def delete_account(self, account_id: str) -> bool:
        with _lock:
            conn = self._conn()
            try:
                cur = conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def create_job(self, job: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        row = {
            "id": job["id"],
            "kind": job.get("kind") or "image",
            "status": job.get("status") or "queued",
            "message": job.get("message") or "",
            "progress": float(job.get("progress") or 0),
            "prompt": job.get("prompt") or "",
            "model": job.get("model") or "",
            "model_version": job.get("model_version") or "",
            "params_json": json.dumps(job.get("params") or {}, ensure_ascii=False),
            "result_json": "",
            "outputs_json": "[]",
            "error": "",
            "traceback": "",
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
        }
        with _lock:
            conn = self._conn()
            try:
                conn.execute(
                    """
                    INSERT INTO jobs (
                        id, kind, status, message, progress, prompt, model, model_version,
                        params_json, result_json, outputs_json, error, traceback,
                        created_at, updated_at, finished_at
                    ) VALUES (
                        :id, :kind, :status, :message, :progress, :prompt, :model, :model_version,
                        :params_json, :result_json, :outputs_json, :error, :traceback,
                        :created_at, :updated_at, :finished_at
                    )
                    """,
                    row,
                )
                conn.commit()
            finally:
                conn.close()
        return self.get_job(row["id"])  # type: ignore[return-value]

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "status",
            "message",
            "progress",
            "result_json",
            "outputs_json",
            "error",
            "traceback",
            "finished_at",
            "prompt",
            "model",
            "model_version",
            "params_json",
        }
        data = {k: v for k, v in fields.items() if k in allowed}
        if "params" in fields:
            data["params_json"] = json.dumps(fields["params"], ensure_ascii=False)
        if "outputs" in fields:
            data["outputs_json"] = json.dumps(fields["outputs"], ensure_ascii=False)
        if "result" in fields:
            data["result_json"] = json.dumps(fields["result"], ensure_ascii=False)
        if not data:
            return self.get_job(job_id)
        data["updated_at"] = time.time()
        data["id"] = job_id
        cols = ", ".join(f"{k}=:{k}" for k in data if k != "id")
        with _lock:
            conn = self._conn()
            try:
                conn.execute(f"UPDATE jobs SET {cols} WHERE id=:id", data)
                conn.commit()
            finally:
                conn.close()
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with _lock:
            conn = self._conn()
            try:
                cur = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
                row = cur.fetchone()
            finally:
                conn.close()
        return self._job_row(row) if row else None

    def list_jobs(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with _lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (int(limit), int(offset)),
                )
                rows = cur.fetchall()
            finally:
                conn.close()
        return [self._job_row(r) for r in rows]

    def delete_job(self, job_id: str) -> bool:
        with _lock:
            conn = self._conn()
            try:
                cur = conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                conn.execute("DELETE FROM api_logs WHERE job_id=?", (job_id,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def clear_all_jobs(self) -> int:
        with _lock:
            conn = self._conn()
            try:
                cur = conn.execute("DELETE FROM jobs")
                conn.execute("DELETE FROM api_logs")
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    def clear_all_logs(self) -> int:
        with _lock:
            conn = self._conn()
            try:
                cur = conn.execute("DELETE FROM api_logs")
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    def add_log(self, **fields: Any) -> int:
        row = {
            "job_id": fields.get("job_id"),
            "phase": fields.get("phase") or "",
            "method": fields.get("method") or "",
            "url": fields.get("url") or "",
            "status_code": fields.get("status_code"),
            "request_headers": _as_json(fields.get("request_headers")),
            "request_body": _as_text(fields.get("request_body")),
            "response_headers": _as_json(fields.get("response_headers")),
            "response_body": _as_text(fields.get("response_body"), limit=20000),
            "error": fields.get("error") or "",
            "duration_ms": fields.get("duration_ms"),
            "created_at": time.time(),
        }
        with _lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    """
                    INSERT INTO api_logs (
                        job_id, phase, method, url, status_code,
                        request_headers, request_body, response_headers, response_body,
                        error, duration_ms, created_at
                    ) VALUES (
                        :job_id, :phase, :method, :url, :status_code,
                        :request_headers, :request_body, :response_headers, :response_body,
                        :error, :duration_ms, :created_at
                    )
                    """,
                    row,
                )
                conn.commit()
                return int(cur.lastrowid or 0)
            finally:
                conn.close()

    def list_logs(
        self,
        *,
        job_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with _lock:
            conn = self._conn()
            try:
                if job_id:
                    cur = conn.execute(
                        "SELECT * FROM api_logs WHERE job_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                        (job_id, int(limit), int(offset)),
                    )
                else:
                    cur = conn.execute(
                        "SELECT * FROM api_logs ORDER BY id DESC LIMIT ? OFFSET ?",
                        (int(limit), int(offset)),
                    )
                rows = cur.fetchall()
            finally:
                conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def _job_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        try:
            d["params"] = json.loads(d.get("params_json") or "{}")
        except Exception:
            d["params"] = {}
        try:
            d["outputs"] = json.loads(d.get("outputs_json") or "[]")
        except Exception:
            d["outputs"] = []
        try:
            d["result"] = json.loads(d["result_json"]) if d.get("result_json") else None
        except Exception:
            d["result"] = None
        # 对外字段
        d["created_at_text"] = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(d.get("created_at") or 0)
        )
        return d


def _as_json(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    try:
        return json.dumps(val, ensure_ascii=False)
    except Exception:
        return str(val)


def _as_text(val: Any, limit: int = 50000) -> str:
    if val is None:
        return ""
    if isinstance(val, (dict, list)):
        text = json.dumps(val, ensure_ascii=False)
    else:
        text = str(val)
    if len(text) > limit:
        return text[:limit] + f"...(truncated {len(text) - limit} chars)"
    return text
