"""Adobe Firefly API 后端（前后端分离）。

- REST JSON API + CORS
- 任务 / 调用日志 → SQLite (data/firefly.db)
- 产物只记录下载 URL，不落盘
"""

from __future__ import annotations

import json
import os
import hmac
import queue
import shutil
import threading
import time
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests

import firefly_pipeline as fp
import video_pipeline as vp
from db import Database
from models_catalog import (
    IMAGE_MODELS,
    VIDEO_MODELS,
    flatten_discovery_models,
    parse_allowed_video_sizes,
    split_by_kind,
    video_capabilities_from_sizes,
)
from token_pool import get_pool

APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"
OUT_DIR = APP_ROOT / "outputs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "firefly.db"
if os.environ.get("FIREFLY_DB_PATH"):
    DB_PATH = Path(os.environ["FIREFLY_DB_PATH"]).expanduser()

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
_cors_origins = os.environ.get("CORS_ORIGINS", "*")
_admin_api_key = (os.environ.get("ADMIN_API_KEY") or "").strip()
_public_mode = os.environ.get("FLASK_PUBLIC") == "1"
CORS(app, resources={r"/api/*": {"origins": _cors_origins.split(",") if _cors_origins != "*" else "*"}})

db = Database(DB_PATH)
_job_queue: queue.Queue[tuple[str, str]] = queue.Queue(
    maxsize=max(1, int(os.environ.get("JOB_QUEUE_SIZE", "24")))
)
_job_workers_started = False
_job_workers_lock = threading.Lock()
_job_worker_count = max(1, int(os.environ.get("JOB_WORKERS", "2")))
_executor_sema = threading.Semaphore(_job_worker_count)
_models_cache: dict[str, Any] = {"ts": 0.0, "data": None, "error": ""}
_model_capabilities_path = DATA_DIR / "model_capabilities.json"
_model_capabilities_lock = threading.Lock()
_credits_cache: dict[str, Any] = {"ts": 0.0, "data": {}}
_account_credits_cache: dict[str, dict[str, Any]] = {}
_credits_refresh_lock = threading.Lock()
_credits_refresh_running = False


@app.before_request
def _require_api_key():
    """可选的 API 保护；公网模式下未配置 key 时默认拒绝 API 请求。"""
    if not request.path.startswith("/api/"):
        return None
    if request.method == "OPTIONS":
        return None
    if not _admin_api_key:
        if _public_mode:
            return jsonify({"error": "公网模式必须配置 ADMIN_API_KEY"}), 503
        return None
    supplied = request.headers.get("X-Admin-Key", "")
    if not supplied:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
    if not supplied or not hmac.compare_digest(supplied, _admin_api_key):
        return jsonify({"error": "需要有效的 ADMIN_API_KEY"}), 401
    return None

# ── models cache ─────────────────────────────────────────────

def _latest_flat_path() -> Path | None:
    files = sorted(
        OUT_DIR.glob("models_flat_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None

def _load_flat_from_disk() -> list[dict[str, Any]] | None:
    import json

    raws = sorted(
        [p for p in OUT_DIR.glob("models_*.json") if "flat" not in p.name],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if raws:
        try:
            families = json.loads(raws[0].read_text(encoding="utf-8"))
            if isinstance(families, list):
                return flatten_discovery_models(families)
        except Exception:
            pass
    # 没有原始 discovery 时才回退旧 flat；原始数据可随解析器升级重新展开。
    path = _latest_flat_path()
    if path:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else None
        except Exception:
            pass
    return None

def _save_flat_to_disk(flat: list[dict[str, Any]], families: list | None = None) -> None:
    import json
    from datetime import datetime

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (OUT_DIR / f"models_flat_{stamp}.json").write_text(
        json.dumps(flat, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if families is not None:
        (OUT_DIR / f"models_{stamp}.json").write_text(
            json.dumps(families, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _load_model_capability_overrides() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_model_capabilities_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _apply_model_capability_overrides(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    overrides = _load_model_capability_overrides()
    if not overrides:
        return items
    merged: list[dict[str, Any]] = []
    for item in items:
        key = f"{item.get('id') or ''}@@{item.get('version') or ''}"
        override = overrides.get(key)
        if not isinstance(override, dict):
            merged.append(item)
            continue
        copy = dict(item)
        for field in ("sizes", "aspect_ratios", "default_aspect_ratio", "sizes_by_aspect"):
            if override.get(field):
                copy[field] = override[field]
        copy["capabilities_source"] = "learned_from_upstream"
        merged.append(copy)
    return merged


def _learn_video_model_capabilities(model: str, version: str, payload: Any) -> bool:
    """从 Adobe 验证响应中学习合法尺寸，仅持久化尺寸与比例。"""
    sizes = parse_allowed_video_sizes(payload)
    if not model or not sizes:
        return False
    key = f"{model}@@{version}"
    with _model_capabilities_lock:
        overrides = _load_model_capability_overrides()
        # Adobe 的 allowed combinations 是该次验证返回的完整白名单，以最新结果覆盖旧缓存。
        capabilities = video_capabilities_from_sizes(sizes)
        current = overrides.get(key) or {}
        capability_fields = (
            "sizes",
            "aspect_ratios",
            "default_aspect_ratio",
            "sizes_by_aspect",
        )
        if all(current.get(field) == capabilities.get(field) for field in capability_fields):
            _models_cache.update(ts=0.0, data=None, error="")
            return True
        capabilities["updated_at"] = time.time()
        overrides[key] = capabilities
        try:
            _model_capabilities_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = _model_capabilities_path.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps(overrides, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(_model_capabilities_path)
        except Exception:
            return False
    _models_cache.update(ts=0.0, data=None, error="")
    return True


def _backfill_model_capabilities_from_logs(limit: int = 200) -> int:
    """从已有失败日志迁移能力缓存，避免为了学习规格再次触发失败任务。"""
    learned = 0
    known = set(_load_model_capability_overrides())
    for job in db.list_jobs(limit=limit):
        if job.get("kind") not in ("video", "video_pipeline"):
            continue
        params = job.get("params") or {}
        if job.get("kind") == "video_pipeline":
            options = params.get("options") or {}
            model = str(options.get("video_model") or "")
            version = str(options.get("video_model_version") or "")
        else:
            model = str(params.get("model") or job.get("model") or "")
            version = str(params.get("model_version") or job.get("model_version") or "")
        if not model:
            continue
        key = f"{model}@@{version}"
        if key in known:
            continue
        for log in db.list_logs(job_id=str(job.get("id") or ""), limit=20):
            payload = " ".join(
                str(value or "")
                for value in (log.get("error"), log.get("response_body"))
            )
            if _learn_video_model_capabilities(model, version, payload):
                learned += 1
                known.add(key)
                break
    return learned

def _fetch_live_models() -> list[dict[str, Any]]:
    token, extras = fp.require_token()
    client = fp.FireflyClient(
        token,
        session=extras.get("_arp_session_id"),
        api_key=extras.get("_api_key"),
        org_id=extras.get("_org_id"),
    )
    try:
        families = client.list_models()
        flat = flatten_discovery_models(families)
        try:
            _save_flat_to_disk(flat, families)
        except Exception:
            pass
        fp.release_token(ok=True, record_stats=False)
        return flat
    except Exception as e:
        fp.release_token(ok=False, error=str(e), record_stats=False)
        raise

def _get_models_by_kind(*, force_live: bool = False) -> tuple[dict[str, list], str, str]:
    now = time.time()
    cached = _models_cache.get("data")
    if cached and not force_live and now - float(_models_cache.get("ts") or 0) < 1800:
        return cached, "memory", str(_models_cache.get("error") or "")

    err = ""
    flat: list[dict[str, Any]] | None = None
    source = ""

    if force_live:
        try:
            flat = _fetch_live_models()
            source = "live"
        except Exception as e:
            err = str(e)

    if flat is None:
        disk = _load_flat_from_disk()
        if disk:
            flat = disk
            source = "disk" if not force_live else "disk_fallback"
        elif not force_live:
            try:
                flat = _fetch_live_models()
                source = "live"
            except Exception as e:
                err = str(e)

    if flat is None:
        flat = IMAGE_MODELS + VIDEO_MODELS
        source = "preset_fallback"

    flat = _apply_model_capability_overrides(flat)
    by_kind = split_by_kind(flat)
    _models_cache["ts"] = now
    _models_cache["data"] = by_kind
    _models_cache["error"] = err
    return by_kind, source, err


def _find_video_model_spec(model: str, version: str = "") -> dict[str, Any] | None:
    """按 modelId + modelVersion 获取 discovery 展开的精确视频能力。"""
    try:
        by_kind, _, _ = _get_models_by_kind(force_live=False)
        pool = (by_kind.get("video") or []) + list(VIDEO_MODELS)
    except Exception:
        pool = list(VIDEO_MODELS)
    return next(
        (
            item for item in pool
            if item.get("id") == model
            and (not version or str(item.get("version") or "") == version)
        ),
        None,
    )


def _video_size_strings(model_spec: dict[str, Any]) -> list[str]:
    sizes = model_spec.get("sizes") or []
    if isinstance(sizes, dict):
        sizes = list(sizes.values())
    if not isinstance(sizes, list):
        return []
    return [
        str(size)
        for size in sizes
        if isinstance(size, str) and "x" in size.lower() and size != "auto"
    ]


def _resolve_video_spec(
    model: str,
    version: str,
    aspect_ratio: Any,
    size: Any,
) -> tuple[str, str, str | None]:
    """用 discovery 能力归一化比例和尺寸，阻止固定 fallback 产生非法请求。"""
    spec = _find_video_model_spec(model, version)
    aspect = str(aspect_ratio or "").strip()
    requested_size = str(size or "").strip().lower()
    if not spec:
        return aspect, requested_size, None

    allowed_aspects = [str(value) for value in (spec.get("aspect_ratios") or []) if value]
    if aspect in ("", "auto"):
        aspect = str(spec.get("default_aspect_ratio") or "")
        if not aspect and allowed_aspects:
            aspect = allowed_aspects[0]
    if allowed_aspects and aspect not in allowed_aspects:
        return aspect, requested_size, (
            f"该模型不支持比例 {aspect}（支持：{allowed_aspects}）"
        )

    size_map = spec.get("sizes_by_aspect") or {}
    if not isinstance(size_map, dict):
        size_map = {}
    # 离线预设历史上把 sizes 直接写成 {aspect: size}。
    if not size_map and isinstance(spec.get("sizes"), dict):
        size_map = spec.get("sizes") or {}
    allowed_sizes = _video_size_strings(spec)
    allowed_size_keys = {value.lower() for value in allowed_sizes}
    preferred_size = str(size_map.get(aspect) or "").lower()

    if requested_size in ("", "auto"):
        requested_size = preferred_size or (allowed_sizes[0].lower() if allowed_sizes else "")
    elif allowed_sizes and requested_size not in allowed_size_keys:
        # 前端缓存过旧时自动纠正为该比例的首个合法尺寸。
        if preferred_size:
            requested_size = preferred_size
        else:
            return aspect, requested_size, (
                f"该模型不支持尺寸 {size}（支持：{allowed_sizes}）"
            )

    # 部分 Adobe discovery 只返回 size 的 $ref，并不展开合法尺寸。此时允许
    # 上游做一次能力探测；若校验响应给出 allowed combinations，任务层会学习
    # 真实规格并立刻用合法尺寸重试同一任务。
    if not requested_size:
        return aspect, "", None
    return aspect, requested_size, None

def _auth_status() -> dict[str, Any]:
    """登录态摘要（公网可读）。完全从 pool 读; 不再回退到 current_token.json / storage.json."""
    pool = get_pool()
    accounts = pool.list()
    return {
        "token_ok": any(a.is_available() for a in accounts),
        "can_ims_refresh": any(a.cookies for a in accounts),
        "pool": pool.status(),
        "mode": "pool",
        "pool_empty": not accounts,
        "hint": (
            "请在「账号池」页面上传 token_file (必填) + cookie_file (可选)."
            if not accounts
            else ""
        ),
    }


def _credits_status() -> dict[str, Any]:
    """读一次额度. 元数据读, 不影响 pool 健康 (失败不 cooldown 账号).

    只有「上游明确 401/403」才视为账号鉴权失败, 给该账号 cooldown.
    token 缺字段 / 网络错 / 上游 5xx 都归为元数据问题, 不动 pool.
    """
    now = time.time()
    cached = _credits_cache.get("data") or {}
    if now - float(_credits_cache.get("ts") or 0) < 30:
        return cached
    label = ""
    try:
        token, extras = fp.require_token()
        label = extras.get("_account_label", "")
        claims = fp.decode_jwt_payload(token)
        account_id = (
            str(claims.get("user_id") or "")
            or str(claims.get("aa_id") or "")
            or str(claims.get("sub") or "")
        ).strip()
        if not account_id:
            # token 解析不出账号 ID — 这是 token 格式问题, 不是账号失效.
            # 释放但不 cooldown, 让账号继续可用于生成.
            fp.release_token(ok=True, record_stats=False)
            data = {
                "error": "当前账号 token 缺 user_id / aa_id / sub, 跳过额度读取",
                "account_label": label,
                "updated_at": now,
            }
            _credits_cache.update(ts=now, data=data)
            return data

        response = requests.get(
            "https://firefly.adobe.io/v1/credits/balance",
            headers={
                "Authorization": f"Bearer {token}",
                "x-api-key": "SunbreakWebUI1",
                "x-account-id": account_id,
                "Origin": "https://new.express.adobe.com",
                "Referer": "https://new.express.adobe.com/",
                "Accept": "application/json",
            },
            timeout=20,
        )
        if response.status_code == 200:
            fp.release_token(ok=True, record_stats=False)
            payload = response.json()
            total_info = payload.get("total") if isinstance(payload, dict) else {}
            quota = total_info.get("quota") if isinstance(total_info, dict) else {}
            data = {
                "total": quota.get("total"),
                "used": quota.get("used"),
                "available": quota.get("available"),
                "available_until": total_info.get("availableUntil"),
                "account_id": extras.get("_account_id", ""),
                "account_label": label,
                "updated_at": now,
                "error": "",
            }
            _credits_cache.update(ts=now, data=data)
            return data

        # 非 200: 只在明确鉴权失败时 cooldown, 其它当元数据问题放过.
        if response.status_code in (401, 403):
            fp.release_token(
                ok=False,
                error=f"credits HTTP {response.status_code}",
                record_stats=False,
            )
            err = f"账号鉴权失败 (HTTP {response.status_code}), 已切换下一个账号"
        else:
            fp.release_token(ok=True, record_stats=False)
            err = f"额度暂不可读取 (HTTP {response.status_code})"
        data = {"error": err, "account_label": label, "updated_at": now}
        _credits_cache.update(ts=now, data=data)
        return data
    except Exception as e:
        # 网络错 / JSON 错 / 任何意外 — 都是元数据问题, 不动 pool.
        fp.release_token(ok=True, record_stats=False)
        data = {"error": f"额度暂不可读取: {e}", "account_label": label, "updated_at": now}
        _credits_cache.update(ts=now, data=data)
        return data


def _refresh_credits_async() -> None:
    """后台刷新额度，避免健康检查被 Adobe 网络请求阻塞。"""
    global _credits_refresh_running
    with _credits_refresh_lock:
        if _credits_refresh_running:
            return
        _credits_refresh_running = True

    def run() -> None:
        global _credits_refresh_running
        try:
            _credits_status()
        finally:
            with _credits_refresh_lock:
                _credits_refresh_running = False

    threading.Thread(target=run, name="credits-refresh", daemon=True).start()


def _credits_snapshot() -> dict[str, Any]:
    now = time.time()
    data = _credits_cache.get("data") or {}
    if now - float(_credits_cache.get("ts") or 0) >= 30:
        _refresh_credits_async()
    return data or {"status": "refreshing", "updated_at": 0}


def _account_credits_status(account) -> dict[str, Any]:
    """读取指定账号额度；仅给账号池管理页展示，不改变 pool 健康状态。"""
    now = time.time()
    cached = _account_credits_cache.get(account.id)
    if cached and now - float(cached.get("ts") or 0) < 30:
        return cached["data"]

    claims = fp.decode_jwt_payload(account.token)
    account_id = (
        str(claims.get("user_id") or "")
        or str(claims.get("aa_id") or "")
        or str(claims.get("sub") or "")
    ).strip()
    if not account_id:
        data = {"error": "token 缺账户 ID，无法读取额度", "updated_at": now}
    else:
        try:
            response = requests.get(
                "https://firefly.adobe.io/v1/credits/balance",
                headers={
                    "Authorization": f"Bearer {account.token}",
                    "x-api-key": "SunbreakWebUI1",
                    "x-account-id": account_id,
                    "Origin": "https://new.express.adobe.com",
                    "Referer": "https://new.express.adobe.com/",
                    "Accept": "application/json",
                },
                timeout=10,
            )
            if response.status_code != 200:
                data = {"error": f"额度暂不可读取 (HTTP {response.status_code})", "updated_at": now}
            else:
                payload = response.json()
                total_info = payload.get("total") if isinstance(payload, dict) else {}
                quota = total_info.get("quota") if isinstance(total_info, dict) else {}
                data = {
                    "total": quota.get("total"),
                    "used": quota.get("used"),
                    "available": quota.get("available"),
                    "available_until": total_info.get("availableUntil"),
                    "updated_at": now,
                    "error": "",
                }
        except Exception as e:
            data = {"error": f"额度暂不可读取: {e}", "updated_at": now}
    _account_credits_cache[account.id] = {"ts": now, "data": data}
    return data

def _public_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if not job:
        return None
    out = {
        k: v
        for k, v in job.items()
        if k
        not in (
            "traceback",
            "result_json",
            "params_json",
            "outputs_json",
            "result",
        )
    }
    files = []
    for o in job.get("outputs") or []:
        if not isinstance(o, dict):
            continue
        url = o.get("url") or ""
        if not url:
            continue
        files.append(
            {
                "url": url,
                "name": url.rsplit("/", 1)[-1][:80],
                "ext": o.get("ext") or "",
                "type": o.get("type") or "",
            }
        )
    out["files"] = files
    out["outputs"] = job.get("outputs") or []
    return out


def _recover_orphaned_jobs() -> int:
    """服务启动时把上一次运行未结束的 queued / running 任务标记为 failed。

    daemon thread 在 gunicorn worker 重启后会直接丢失，没有续跑能力；
    把它们标成 failed + 「服务重启，任务中断」让用户知道要重新提交。
    """
    affected = 0
    try:
        rows = db.list_jobs(limit=500, offset=0)
    except Exception as e:
        sys.stderr.write(f"[startup] recover: list_jobs failed: {e}\n")
        return 0
    for job in rows:
        if job.get("status") not in ("queued", "running"):
            continue
        try:
            db.update_job(
                job["id"],
                status="failed",
                message="服务重启，任务中断，请重新提交。",
                progress=100,
                finished_at=time.time(),
            )
            affected += 1
        except Exception as e:
            sys.stderr.write(f"[startup] recover: {job.get('id')} update failed: {e}\n")
    if affected:
        sys.stderr.write(f"[startup] recovered {affected} orphaned jobs (queued/running → failed)\n")
    return affected

# ── job runner ───────────────────────────────────────────────

def _job_worker_loop() -> None:
    while True:
        job_id, kind = _job_queue.get()
        try:
            if kind == "video_pipeline":
                _run_video_job(job_id)
            else:
                _run_job(job_id)
        except Exception:
            # 运行函数内部会更新任务状态；这里避免 worker 线程退出。
            traceback.print_exc(file=sys.stderr)
        finally:
            _job_queue.task_done()


def _ensure_job_workers() -> None:
    global _job_workers_started
    if _job_workers_started:
        return
    with _job_workers_lock:
        if _job_workers_started:
            return
        for index in range(_job_worker_count):
            threading.Thread(
                target=_job_worker_loop,
                name=f"firefly-job-{index + 1}",
                daemon=True,
            ).start()
        _job_workers_started = True


def _enqueue_job(job_id: str, kind: str) -> bool:
    _ensure_job_workers()
    try:
        _job_queue.put_nowait((job_id, kind))
        return True
    except queue.Full:
        return False

def _run_job(job_id: str) -> None:
    with _executor_sema:
        job = db.get_job(job_id) or {}
        params = job.get("params") or {}
        kind = params.get("kind") or job.get("kind") or "image"
        db.update_job(job_id, status="running", message="提交上游任务…", progress=5)
        t0 = time.time()
        submit_started = time.time()
        submitted = {"ok": False}
        try:
            prompt = str(params.get("prompt") or "").strip()
            if not prompt:
                raise RuntimeError("prompt 不能为空")
            model = str(params.get("model") or "").strip()
            version = str(params.get("model_version") or "").strip()
            n = max(1, min(int(params.get("n") or 1), 4))
            seeds_raw = str(params.get("seeds") or "").strip()
            seeds = (
                [int(x) for x in seeds_raw.split(",") if x.strip()]
                if seeds_raw
                else None
            )

            # ── (1) 请求参数 ─────────────────────────────────
            db.add_log(
                job_id=job_id,
                phase="request_params",
                method="INTERNAL",
                url=f"generate/{kind}",
                request_body=params,
                status_code=0,
            )

            def on_submitted(task_id: str, poll_url: str, status_code: int, body: dict[str, Any]) -> None:
                # ── (2) 创建任务后的返回信息 ─────────────────────
                submitted["ok"] = True
                submitted["task_id"] = task_id
                submitted["poll_url"] = poll_url
                submitted["status_code"] = status_code
                submitted["body"] = body
                db.add_log(
                    job_id=job_id,
                    phase="task_created",
                    method="INTERNAL",
                    url=f"generate/{kind}",
                    status_code=status_code or 0,
                    response_body={
                        "task_id": task_id,
                        "poll_url": poll_url,
                        "account_id": fp.current_account_id(),
                        "account_label": fp.current_account_label(),
                        "upstream": body,
                    },
                    duration_ms=(time.time() - submit_started) * 1000,
                )
                db.update_job(
                    job_id,
                    status="running",
                    message="上游已受理，正在轮询结果…",
                    progress=10,
                )

            def run_upstream() -> dict[str, Any]:
                if kind == "image":
                    size = str(params.get("size") or "auto")
                    detail = int(params.get("detail_level") or 3)
                    db.update_job(job_id, message="生成图片中…", progress=15)
                    return fp.generate_image(
                        prompt,
                        model=model,
                        model_version=version,
                        n=n,
                        size=size,
                        seeds=seeds,
                        detail_level=detail,
                        poll_interval=float(params.get("poll_interval") or 4),
                        max_wait=float(params.get("max_wait") or 900),
                        download_dir=None,
                        on_submitted=on_submitted,
                    )

                duration = params.get("duration")
                duration_i = int(duration) if duration not in (None, "", 0) else None
                aspect = str(params.get("aspect_ratio") or "16:9").strip() or "16:9"
                size = params.get("size")
                if not size:
                    size = dict(
                        fp.VIDEO_SIZE_BY_ASPECT.get(aspect)
                        or fp.VIDEO_SIZE_BY_ASPECT["16:9"]
                    )
                db.update_job(job_id, message="生成视频中…", progress=15)
                return fp.generate_video(
                    prompt,
                    model=model,
                    model_version=version,
                    n=n,
                    seeds=seeds,
                    duration=duration_i,
                    size=size,
                    aspect_ratio=aspect,
                    generate_audio=bool(params.get("generate_audio", True)),
                    negative_prompt=str(params.get("negative_prompt") or "").strip(),
                    poll_interval=float(params.get("poll_interval") or 6),
                    max_wait=float(params.get("max_wait") or 1800),
                    download_dir=None,
                    on_submitted=on_submitted,
                )

            try:
                data = run_upstream()
            except Exception as first_error:
                learned_spec = kind == "video" and _learn_video_model_capabilities(
                    model,
                    version,
                    first_error,
                )
                if learned_spec:
                    resolved_aspect, resolved_size, spec_error = _resolve_video_spec(
                        model,
                        version,
                        params.get("aspect_ratio"),
                        params.get("size"),
                    )
                    if spec_error or not resolved_size:
                        raise first_error
                    params["aspect_ratio"] = resolved_aspect
                    params["size"] = resolved_size
                    submitted["ok"] = False
                    db.add_log(
                        job_id=job_id,
                        phase="task_retry",
                        method="INTERNAL",
                        url=f"generate/{kind}",
                        status_code=0,
                        response_body={
                            "attempt": 1,
                            "reason": "model_capabilities_learned",
                            "aspect_ratio": resolved_aspect,
                            "size": resolved_size,
                        },
                        duration_ms=(time.time() - t0) * 1000,
                    )
                    db.update_job(
                        job_id,
                        status="running",
                        message=f"已识别模型规格，改用 {resolved_size} 自动重试…",
                        progress=15,
                        params=params,
                    )
                    data = run_upstream()
                else:
                    retryable = fp.is_retryable_upstream_error(first_error)
                    terminal_failure = isinstance(first_error, fp.UpstreamTaskFailed)
                    # 已受理任务一般只轮询，避免重复生成；但上游明确给出额度/鉴权/
                    # 限流等可重试终态时，换号重投一次。
                    if not retryable or (submitted["ok"] and not terminal_failure):
                        raise
                    # 第一次临时失败: 当前账号入冷却并释放，下一次 run_upstream 会重新取号。
                    fp.release_token(ok=False, error=str(first_error))
                    db.add_log(
                        job_id=job_id,
                        phase="task_retry",
                        method="INTERNAL",
                        url=f"generate/{kind}",
                        status_code=0,
                        response_body={
                            "attempt": 1,
                            "reason": type(first_error).__name__,
                            "message": str(first_error)[:240],
                            "terminal_failure": terminal_failure,
                        },
                        duration_ms=(time.time() - t0) * 1000,
                    )
                    db.update_job(
                        job_id,
                        status="running",
                        message="上游失败，切换账号后自动重试…",
                        progress=15,
                    )
                    data = run_upstream()

            outputs = data.get("outputs") or []
            url_count = sum(1 for o in outputs if o.get("url"))
            db.update_job(
                job_id,
                status="succeeded",
                message=f"完成，{url_count} 个下载地址",
                progress=100,
                outputs=outputs,
                result=data.get("result"),
                finished_at=time.time(),
            )
            # 上游完整成功 → 当前账号 OK, 释放给池子.
            fp.release_token(ok=True)
            # ── (3a) 创建成功：返回文件地址 ─────────────────────
            db.add_log(
                job_id=job_id,
                phase="task_succeeded",
                method="INTERNAL",
                url=f"generate/{kind}",
                status_code=200,
                response_body={
                    "task_id": data.get("task_id"),
                    "poll_url": data.get("poll_url"),
                    "account": data.get("account_label") or "",
                    "files": [
                        {
                            "type": o.get("type"),
                            "url": o.get("url"),
                            "blob": o.get("blob"),
                            "ext": o.get("ext"),
                        }
                        for o in outputs
                    ],
                    "file_count": len(outputs),
                    "url_count": url_count,
                },
                duration_ms=(time.time() - t0) * 1000,
            )
        except Exception as e:
            tb = traceback.format_exc()
            err_msg = str(e)
            if kind == "video":
                _learn_video_model_capabilities(
                    str(params.get("model") or ""),
                    str(params.get("model_version") or ""),
                    err_msg,
                )
            user_msg = fp.summarize_upstream_error(e)
            upstream_error = fp.upstream_error_details(e)
            # 把异常归类上报给连接池: 401/403/quota 等会自动 cooldown, 下一请求换账号.
            fp.release_token(ok=False, error=err_msg)
            # 完整堆栈只打印到服务器 stderr, 不写进任何用户可见字段。
            sys.stderr.write(f"[JOB FAILED] {job_id} ({kind}) {type(e).__name__}: {err_msg}\n")
            traceback.print_exc(file=sys.stderr)
            db.update_job(
                job_id,
                status="failed",
                message=user_msg,
                progress=100,
                error=user_msg,
                finished_at=time.time(),
            )
            # ── (3b) 创建失败：异常原因 ─────────────────────────
            log_payload = {
                "user_message": user_msg,
                "error_type": type(e).__name__,
                "account_id": fp.current_account_id() or "",
            }
            if isinstance(e, fp.UpstreamTaskFailed):
                # 终态失败的上游 payload 是定位额度/权限/审核问题所需的最小证据。
                log_payload["upstream_terminal"] = {
                    k: e.payload.get(k)
                    for k in ("status", "state", "status_code", "code", "error", "message", "reason")
                    if e.payload.get(k) is not None
                }
            if upstream_error:
                log_payload["upstream_error"] = upstream_error
            db.add_log(
                job_id=job_id,
                phase="task_failed",
                method="INTERNAL",
                url=f"generate/{kind}",
                status_code=500,
                error=user_msg,
                response_body=log_payload,
                duration_ms=(time.time() - t0) * 1000,
            )
            # 完整堆栈只打印到服务器 stderr, 不再落 DB, 避免在日志页泄露内部错误信息。
            # 运维调试请看 /tmp/firefly-studio-api.log。

# ── routes ───────────────────────────────────────────────────

def _page_arg(raw: str | None, default: int, *, maximum: int) -> int:
    try:
        value = int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        value = default
    return max(0, min(value, maximum))

@app.get("/outputs/<path:filename>")
def api_outputs(filename: str):
    """从 outputs/ 目录读取文件（成片 / TTS / 关键帧）。"""
    from flask import send_from_directory, abort
    base = OUT_DIR.resolve()
    target = (base / filename).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        abort(404)
    if not target.exists() or not target.is_file():
        abort(404)
    return send_from_directory(base, filename, conditional=True)


def _output_url(path: str | None) -> str:
    """把服务端绝对路径转换为不泄露文件系统结构的公共 URL。"""
    if not path:
        return ""
    base = OUT_DIR.resolve()
    try:
        rel = Path(path).resolve().relative_to(base)
    except (OSError, ValueError):
        return ""
    return "/outputs/" + quote(rel.as_posix(), safe="/")


def _cleanup_job_artifacts(job_id: str) -> None:
    """删除指定成片任务的本地中间文件，且严格限制在 outputs 子目录内。"""
    for root in (OUT_DIR / "videos", OUT_DIR / "tts"):
        root = root.resolve()
        target = (root / job_id).resolve()
        if target.parent != root or not target.is_dir():
            continue
        try:
            shutil.rmtree(target)
        except OSError as exc:
            sys.stderr.write(f"[cleanup] {target}: {exc}\n")

@app.get("/api/health")
def api_health():
    """轻量健康检查：登录态 + 额度摘要。不暴露 DB 路径、token 元信息。"""
    return jsonify(
        {
            "ok": True,
            "auth": _auth_status(),
            "credits": _credits_snapshot(),
            "time": time.time(),
        }
    )

@app.get("/api/models")
def api_models():
    use_preset = request.args.get("preset") in ("1", "true", "yes")
    force = request.args.get("refresh") in ("1", "true", "yes")
    kind_filter = str(request.args.get("kind") or "").strip().lower()
    q = str(request.args.get("q") or "").strip().lower()

    if use_preset:
        by_kind = split_by_kind(IMAGE_MODELS + VIDEO_MODELS)
        source, err = "preset", ""
    else:
        by_kind, source, err = _get_models_by_kind(force_live=force)

    def _filter(items: list[dict]) -> list[dict]:
        if not q:
            return items
        out = []
        for m in items:
            blob = " ".join(
                str(m.get(k) or "")
                for k in ("id", "version", "label", "family", "provider", "kind")
            ).lower()
            if q in blob:
                out.append(m)
        return out

    if kind_filter and kind_filter in by_kind:
        presets = {kind_filter: _filter(by_kind[kind_filter])}
    else:
        presets = {k: _filter(v) for k, v in by_kind.items()}

    return jsonify(
        {
            "source": source,
            "live_error": err or None,
            "presets": presets,
            "counts": {k: len(v) for k, v in by_kind.items()},
            "total": sum(len(v) for v in by_kind.values()),
        }
    )

@app.post("/api/generate")
def api_generate():
    body = request.get_json(force=True, silent=True) or {}
    kind = str(body.get("kind") or "image").strip().lower()
    if kind not in ("image", "video"):
        return jsonify({"error": "kind 必须是 image 或 video"}), 400
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "请输入提示词"}), 400
    model = str(body.get("model") or "").strip()
    version = str(body.get("model_version") or body.get("version") or "").strip()
    if not model:
        return jsonify({"error": "请选择模型"}), 400
    if not version:
        pool = IMAGE_MODELS if kind == "image" else VIDEO_MODELS
        hit = next((m for m in pool if m["id"] == model), None)
        version = str((hit or {}).get("version") or ("2" if kind == "image" else "1"))

    resolved_aspect = str(body.get("aspect_ratio") or "").strip()
    resolved_size = body.get("size") or ("auto" if kind == "image" else "")
    if kind == "video":
        resolved_aspect, resolved_size, spec_error = _resolve_video_spec(
            model,
            version,
            resolved_aspect,
            resolved_size,
        )
        if spec_error:
            return jsonify({"error": spec_error}), 400

    auth = _auth_status()
    if not auth.get("token_ok"):
        return (
            jsonify(
                {
                    "error": "账号池为空. 请到「账号池」页面上传 token_file "
                    "(必填) + cookie_file (可选).",
                    "auth": auth,
                }
            ),
            400,
        )

    job_id = uuid.uuid4().hex[:12]
    params = {
        "kind": kind,
        "prompt": prompt,
        "model": model,
        "model_version": version,
        "n": body.get("n", 1),
        "size": resolved_size,
        "detail_level": body.get("detail_level", 3),
        "duration": body.get("duration"),
        "aspect_ratio": resolved_aspect,
        "generate_audio": body.get("generate_audio", True),
        "negative_prompt": body.get("negative_prompt") or "",
        "seeds": body.get("seeds") or "",
        "max_wait": body.get("max_wait") or (900 if kind == "image" else 1800),
    }
    job = db.create_job(
        {
            "id": job_id,
            "kind": kind,
            "status": "queued",
            "message": "排队中",
            "progress": 0,
            "prompt": prompt,
            "model": model,
            "model_version": version,
            "params": params,
        }
    )
    db.add_log(
        job_id=job_id,
        phase="api_generate",
        method="POST",
        url="/api/generate",
        status_code=202,
        request_body=params,
    )
    if not _enqueue_job(job_id, "generate"):
        db.update_job(
            job_id,
            status="failed",
            message="任务队列已满，请稍后重试。",
            progress=100,
            finished_at=time.time(),
        )
        return jsonify({"error": "任务队列已满，请稍后重试。", "job_id": job_id}), 429
    return jsonify({"job_id": job_id, "job": _public_job(job)}), 202

@app.get("/api/jobs")
def api_jobs():
    limit = _page_arg(request.args.get("limit"), 50, maximum=100)
    offset = _page_arg(request.args.get("offset"), 0, maximum=1_000_000)
    items = db.list_jobs(limit=limit, offset=offset)
    return jsonify({"jobs": [_public_job(j) for j in items], "count": len(items)})

@app.get("/api/jobs/<job_id>")
def api_job(job_id: str):
    job = db.get_job(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify({"job": _public_job(job)})

@app.delete("/api/jobs/<job_id>")
def api_job_delete(job_id: str):
    job = db.get_job(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    ok = db.delete_job(job_id)
    if not ok:
        return jsonify({"error": "job not found"}), 404
    _cleanup_job_artifacts(job_id)
    return jsonify({"ok": True})

@app.get("/api/logs")
def api_logs():
    job_id = request.args.get("job_id") or None
    limit = _page_arg(request.args.get("limit"), 100, maximum=200)
    offset = _page_arg(request.args.get("offset"), 0, maximum=1_000_000)
    items = db.list_logs(job_id=job_id, limit=limit, offset=offset)
    return jsonify({"logs": items, "count": len(items)})

@app.delete("/api/logs")
def api_logs_clear():
    """清空所有调用日志（不影响 jobs 表）。"""
    deleted = db.clear_all_logs()
    return jsonify({"ok": True, "deleted": deleted})

@app.post("/api/chat/clear")
def api_chat_clear():
    """清空聊天记录：删除所有 jobs 及其相关 logs。

    用于「清空对话」按钮；API 调用安全、可重入。
    """
    deleted = db.clear_all_jobs()
    return jsonify({"ok": True, "deleted": deleted})

@app.get("/api/voices")
def api_voices():
    """列出外部 TTS 可用人声；上游失败 → 兜底常用语音列表。"""
    try:
        client = vp.TTSClient()
        voices = client.list_voices()
    except Exception:
        voices = []
    if not voices:
        voices = [
            {"ShortName": vp.TTS_DEFAULT_VOICE, "Gender": "Female", "Locale": "zh-CN"},
            {"ShortName": vp.TTS_FALLBACK_VOICE, "Gender": "Female", "Locale": "en-US"},
            {"ShortName": "zh-CN-YunxiNeural", "Gender": "Male", "Locale": "zh-CN"},
            {"ShortName": "en-US-GuyNeural", "Gender": "Male", "Locale": "en-US"},
        ]
    # 精简字段，省字节
    slim = [
        {
            "id": v.get("ShortName") or v.get("id") or "",
            "name": v.get("FriendlyName") or v.get("Name") or "",
            "gender": v.get("Gender") or "",
            "locale": v.get("Locale") or "",
        }
        for v in voices
        if (v.get("ShortName") or v.get("id"))
    ]
    return jsonify({"voices": slim, "count": len(slim), "source": "edge" if voices else "preset"})


@app.get("/api/llm-models")
def api_llm_models():
    """代理 OpenAI 兼容服务的 /v1/models，供分镜模型下拉选择。"""
    cfg = vp._load_llm_config()
    try:
        response = requests.get(
            f"{cfg.base_url}/models",
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            timeout=cfg.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        raw = payload.get("data") if isinstance(payload, dict) else []
        models = sorted(
            {
                str(item.get("id") or "").strip()
                for item in raw
                if isinstance(item, dict) and item.get("id")
            }
        )
        return jsonify({"models": models, "count": len(models), "default": cfg.model})
    except Exception as e:
        return jsonify({"models": [], "count": 0, "default": cfg.model, "error": str(e)}), 502


# ── accounts pool ────────────────────────────────────────────

_ALLOWED_UPLOAD_SUFFIX = {".json"}
_MAX_UPLOAD_BYTES = 512 * 1024  # 512 KB 上限 (cookie 文件通常 < 200KB)


def _read_upload(file_storage, *, field: str) -> dict[str, Any] | None:
    """读 multipart 上传的 JSON 文件. 失败抛 ValueError."""
    import json

    if file_storage is None or not file_storage.filename:
        return None
    name = str(file_storage.filename)
    suffix = Path(name).suffix.lower()
    if suffix not in _ALLOWED_UPLOAD_SUFFIX:
        raise ValueError(f"{field}: 仅支持 .json 文件 (收到 {name})")
    raw = file_storage.read(_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise ValueError(f"{field}: 文件超过 {_MAX_UPLOAD_BYTES // 1024}KB 上限")
    try:
        data = json.loads(raw.decode("utf-8-sig") or "{}")
    except Exception as e:
        raise ValueError(f"{field}: JSON 解析失败: {e}")
    if not isinstance(data, dict):
        raise ValueError(f"{field}: 顶层必须是 JSON 对象")
    return data


@app.get("/api/accounts")
def api_accounts_list():
    """列出所有账号 (含健康状态). 不返回 token / cookies 原文."""
    pool = get_pool()
    items = []
    for account in pool.list():
        item = account.public_dict()
        item["credits"] = (
            _account_credits_status(account)
            if item["healthy"]
            else {"error": "账号当前不可用", "updated_at": 0}
        )
        items.append(item)
    return jsonify({"accounts": items, "count": len(items), "pool": pool.status()})


@app.post("/api/accounts/upload")
def api_accounts_upload():
    """上传 token / cookie；cookie-only 时立即走 IMS 换 token 后入池。"""
    label = (request.form.get("label") or "").strip()
    if not label:
        return jsonify({"error": "label 不能为空"}), 400
    try:
        token_payload = _read_upload(request.files.get("token_file"), field="token_file")
        cookie_payload = _read_upload(request.files.get("cookie_file"), field="cookie_file")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not token_payload and not cookie_payload:
        return jsonify({"error": "请至少上传 token_file 或 cookie_file"}), 400

    cookies: list[dict[str, Any]] = []
    if cookie_payload:
        # storage.json 格式: {"cookies": [...]}; 兼容只放 cookies 数组的形式
        if isinstance(cookie_payload.get("cookies"), list):
            cookies = [c for c in cookie_payload["cookies"] if isinstance(c, dict)]
        elif isinstance(cookie_payload.get("cookies"), dict):
            # playwright 单 cookie dict 转 list
            cookies = [cookie_payload["cookies"]]

    try:
        if token_payload:
            acct = get_pool().add_from_files(
                token_payload=token_payload,
                cookies=cookies,
                label=label,
            )
        else:
            if not cookies:
                return jsonify({"error": "cookie_file 中找不到 cookies 数组"}), 400
            refreshed = fp.ims_refresh_token(
                client_id=fp.API_KEY_CLIO,
                origin=fp.ORIGIN_FIREFLY,
                cookies=cookies,
            )
            if not refreshed:
                return jsonify({"error": "cookie 无法换取 IMS token，请确认已登录且未过期"}), 400
            token, meta = refreshed
            claims = fp.decode_jwt_payload(token)
            acct = get_pool().add(
                token=token,
                label=label,
                cookies=cookies,
                client_id=str(claims.get("client_id") or fp.API_KEY_CLIO),
                expires_at=time.time() + int(meta.get("expires_in") or 86000),
                source="cookie_refresh",
            )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "account": acct.public_dict()}), 201


# 必须先注册字面量路由, 不然 Flask 把它当 <account_id> 匹配后返回 405.
@app.post("/api/accounts/migrate-legacy")
def api_accounts_migrate_legacy_removed():
    """占位: 旧的一键迁移端点已停用. 改用 POST /api/accounts/upload."""
    return jsonify({
        "error": "该端点已停用. 请用 POST /api/accounts/upload 上传 token_file.",
    }), 410


@app.delete("/api/accounts/<account_id>")
def api_accounts_delete(account_id: str):
    pool = get_pool()
    if not pool.get(account_id):
        return jsonify({"error": "account not found"}), 404
    pool.remove(account_id)
    return jsonify({"ok": True})


@app.patch("/api/accounts/<account_id>")
def api_accounts_patch(account_id: str):
    body = request.get_json(force=True, silent=True) or {}
    label = body.get("label")
    disabled = body.get("disabled")
    try:
        acct = get_pool().update(
            account_id,
            label=str(label).strip() if isinstance(label, str) else None,
            disabled=bool(disabled) if isinstance(disabled, bool) else None,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not acct:
        return jsonify({"error": "account not found"}), 404
    return jsonify({"ok": True, "account": acct.public_dict()})


@app.post("/api/accounts/<account_id>/refresh")
def api_accounts_refresh(account_id: str):
    """用账号自带的 cookies 调 IMS check/v6 刷 token; 成功则覆盖 token + 重置冷却."""
    pool = get_pool()
    acct = pool.get(account_id)
    if not acct:
        return jsonify({"error": "account not found"}), 404
    if not acct.cookies:
        return jsonify({"error": "该账号未提供 cookie 文件, 无法 IMS 刷新"}), 400

    prefer = acct.client_id or fp.API_KEY_CLIO
    origin = fp.ORIGIN_EXPRESS if prefer == fp.API_KEY_PROJECTX else fp.ORIGIN_FIREFLY
    refreshed = fp.ims_refresh_token(
        client_id=prefer,
        origin=origin,
        cookies=acct.cookies,
    )

    if not refreshed:
        pool.mark_refresh_failure(
            account_id,
            seconds=60,
            error="IMS refresh failed (cookie expired?)",
        )
        return jsonify({"ok": False, "error": "IMS 刷新失败 (cookie 已过期?)"}), 502

    tok, meta = refreshed
    claims = fp.decode_jwt_payload(tok)
    exp_in = int(meta.get("expires_in") or 86000)
    new_cid = str(claims.get("client_id") or acct.client_id or prefer)
    pool.set_token(
        account_id,
        token=tok,
        expires_at=time.time() + exp_in,
        client_id=new_cid,
    )
    # 刷新成功 → 立即给账号解除 cooldown
    pool.mark_refreshed(account_id)
    fresh = pool.get(account_id)
    return jsonify({"ok": True, "account": fresh.public_dict() if fresh else None})


def _run_video_job(job_id: str) -> None:
    """后台跑 video_pipeline.generate_full_video，并把结果写回 SQLite。"""
    with _executor_sema:
        job = db.get_job(job_id) or {}
        params = job.get("params") or {}
        prompt = str(params.get("prompt") or "").strip()
        if not prompt:
            db.update_job(job_id, status="failed", message="prompt 不能为空", progress=100, finished_at=time.time())
            return
        db.update_job(job_id, status="running", message="规划分镜中…", progress=5)

        def _on_progress(pct: float, msg: str) -> None:
            try:
                db.update_job(
                    job_id,
                    status="running",
                    message=msg,
                    progress=max(5.0, min(float(pct), 100.0)),
                )
            except Exception:
                pass

        def _on_state(shots: list[dict[str, Any]], msg: str) -> None:
            try:
                db.update_job(
                    job_id,
                    status="running",
                    message=msg,
                    result={"shots": shots},
                )
                db.add_log(
                    job_id=job_id,
                    phase="video_progress",
                    method="INTERNAL",
                    url="video_pipeline",
                    status_code=0,
                    response_body={"message": msg, "shots": shots},
                )
            except Exception:
                pass

        t0 = time.time()
        try:
            db.add_log(
                job_id=job_id, phase="video_request",
                method="INTERNAL", url="video_pipeline",
                status_code=0, request_body={"prompt": prompt, "options": params.get("options") or {}},
            )

            def recover_model_spec(
                model: str,
                version: str,
                aspect: str,
                error: Any,
            ) -> tuple[str, Any] | None:
                if not _learn_video_model_capabilities(model, version, error):
                    return None
                resolved_aspect, resolved_size, spec_error = _resolve_video_spec(
                    model,
                    version,
                    aspect,
                    "",
                )
                if spec_error or not resolved_size:
                    return None
                options = params.get("options") or {}
                options["aspect_ratio"] = resolved_aspect
                options["video_size"] = resolved_size
                params["options"] = options
                db.update_job(
                    job_id,
                    status="running",
                    message=f"已识别模型规格，改用 {resolved_size} 自动重试当前分镜…",
                    params=params,
                )
                db.add_log(
                    job_id=job_id,
                    phase="video_retry",
                    method="INTERNAL",
                    url="video_pipeline",
                    status_code=0,
                    response_body={
                        "reason": "model_capabilities_learned",
                        "aspect_ratio": resolved_aspect,
                        "size": resolved_size,
                    },
                    duration_ms=(time.time() - t0) * 1000,
                )
                return resolved_aspect, resolved_size

            def run_video() -> dict[str, Any]:
                return vp.generate_full_video(
                    prompt,
                    options_dict=params.get("options") or {},
                    on_progress=_on_progress,
                    on_state=_on_state,
                    recover_model_spec=recover_model_spec,
                    job_id=job_id,
                )

            try:
                result = run_video()
            except Exception as first_error:
                if not fp.is_retryable_upstream_error(first_error):
                    raise
                fp.release_token(ok=False, error=str(first_error))
                db.add_log(
                    job_id=job_id, phase="video_retry",
                    method="INTERNAL", url="video_pipeline",
                    status_code=0,
                    response_body={
                        "attempt": 1,
                        "reason": type(first_error).__name__,
                        "message": str(first_error)[:240],
                    },
                    duration_ms=(time.time() - t0) * 1000,
                )
                db.update_job(
                    job_id,
                    status="running",
                    message="上游失败，切换账号后自动重试…",
                    progress=5,
                )
                result = run_video()
            shots = result.get("shots") or []
            # 把分镜 URL 也作为 outputs 暴露，前端现有 video 预览逻辑就能用
            outputs: list[dict[str, Any]] = []
            for s in shots:
                if s.get("video_url"):
                    outputs.append({
                        "type": "video",
                        "url": s["video_url"],
                        "ext": ".mp4",
                        "label": f"分镜 {s.get('index')}",
                    })
                if s.get("image_url"):
                    outputs.append({
                        "type": "image",
                        "url": s["image_url"],
                        "ext": ".jpg",
                        "label": f"分镜 {s.get('index')} 关键帧",
                    })
            final_path = result.get("final_video_path") or ""
            if final_path:
                # 本地文件：暴露为 /outputs/videos/<job>/final.mp4；前端 video 标签能直接播
                rel = final_path.split("/outputs/", 1)[-1] if "/outputs/" in final_path else ""
                if rel:
                    outputs.append({
                        "type": "video",
                        "url": f"/outputs/{rel}",
                        "ext": ".mp4",
                        "label": "成片",
                        "local": True,
                    })

            errs = result.get("errors") or []
            video_options = params.get("options") or {}
            for error in errs:
                _learn_video_model_capabilities(
                    str(video_options.get("video_model") or ""),
                    str(video_options.get("video_model_version") or ""),
                    error,
                )
            # 只有「最终 mp4 存在」才算 succeeded；其它统一 failed
            final_exists = bool(final_path) and Path(final_path).is_file()
            status = "succeeded" if final_exists else "failed"
            if final_exists:
                duration = result.get("ffprobe_duration_total") or 0
                message = (
                    f"完成 {len(shots)} 个分镜"
                    + (f"，{round(duration, 1)}s" if duration else "")
                    + (f"（部分失败：{len(errs)} 项）" if errs else "")
                )
            elif shots:
                message = (
                    f"分镜已生成 {len(shots)} 个，最终合成失败"
                    + (f"（{len(errs)} 项错误）" if errs else "")
                )
            else:
                message = "无有效分镜，最终视频未生成"
            db.update_job(
                job_id,
                status=status,
                message=message,
                progress=100,
                outputs=outputs,
                result={
                    "final_video_path": final_path if final_exists else "",
                    "manifest_path": result.get("manifest_path") or "",
                    "shots": shots,
                    "tts_segments": result.get("tts_segments") or [],
                    "used_ffmpeg": bool(result.get("used_ffmpeg")),
                    "ffprobe_duration_total": result.get("ffprobe_duration_total") or 0,
                    "errors": errs,
                },
                finished_at=time.time(),
            )
            fp.release_token(ok=True)
            db.add_log(
                job_id=job_id, phase="video_succeeded",
                method="INTERNAL", url="video_pipeline",
                status_code=200,
                response_body={
                    "shots": len(shots),
                    "final_video_path": final_path,
                    "duration_sec": result.get("ffprobe_duration_total") or 0,
                    "errors": errs,
                    "account": fp.current_account_label(),
                },
                duration_ms=(time.time() - t0) * 1000,
            )
        except Exception as e:
            tb = traceback.format_exc()
            err_msg = str(e)
            video_options = params.get("options") or {}
            _learn_video_model_capabilities(
                str(video_options.get("video_model") or ""),
                str(video_options.get("video_model_version") or ""),
                err_msg,
            )
            fp.release_token(ok=False, error=err_msg)
            # TTS 错误用专门文案；其它走 fp.summarize_upstream_error
            if "TTS" in err_msg or "tts" in err_msg.lower():
                user_msg = vp.summarize_tts_error(err_msg)
            else:
                user_msg = fp.summarize_upstream_error(e)
            upstream_error = fp.upstream_error_details(e)
            sys.stderr.write(f"[VIDEO JOB FAILED] {job_id} {type(e).__name__}: {err_msg}\n")
            traceback.print_exc(file=sys.stderr)
            db.update_job(
                job_id,
                status="failed",
                message=user_msg,
                progress=100,
                error=user_msg,
                finished_at=time.time(),
            )
            video_log_payload: dict[str, Any] = {
                "error_type": type(e).__name__,
                "account_id": fp.current_account_id() or "",
            }
            if isinstance(e, fp.UpstreamTaskFailed):
                video_log_payload["upstream_terminal"] = {
                    k: e.payload.get(k)
                    for k in ("status", "state", "status_code", "code", "error", "message", "reason")
                    if e.payload.get(k) is not None
                }
            if upstream_error:
                video_log_payload["upstream_error"] = upstream_error
            db.add_log(
                job_id=job_id, phase="video_failed",
                method="INTERNAL", url="video_pipeline",
                status_code=500,
                error=user_msg,
                response_body=video_log_payload,
                duration_ms=(time.time() - t0) * 1000,
            )

def _validate_video_options(options: dict[str, Any]) -> str | None:
    """按 discovery 能力校验并补全一键成片的视频规格。"""
    model = str(options.get("video_model") or "").strip()
    if not model:
        return None
    version = str(options.get("video_model_version") or "").strip()
    m = _find_video_model_spec(model, version)
    if not m:
        return None  # 未知模型让 fp 兜底，不在前端枚举过的也允许走
    dur = options.get("duration_sec")
    if dur is not None:
        try:
            dur_i = int(dur)
        except (TypeError, ValueError):
            return f"时长参数非法：{dur}"
        allowed = m.get("durations") or []
        if allowed and dur_i not in allowed:
            return f"该模型不支持时长 {dur_i}s（支持：{allowed}）"
    aspect, size, spec_error = _resolve_video_spec(
        model,
        version,
        options.get("aspect_ratio"),
        options.get("video_size"),
    )
    if spec_error:
        return spec_error
    options["aspect_ratio"] = aspect
    options["video_size"] = size
    return None


@app.post("/api/video/generate")
def api_video_generate():
    """提交一条「文字 → 多镜头成片」任务；异步返回 job_id。"""
    body = request.get_json(force=True, silent=True) or {}
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "请输入提示词"}), 400
    options = body.get("options") or {}
    if not isinstance(options, dict):
        return jsonify({"error": "options 必须是对象"}), 400

    err = _validate_video_options(options)
    if err:
        return jsonify({"error": err}), 400

    auth = _auth_status()
    if not auth.get("token_ok"):
        return (
            jsonify({
                "error": "账号池为空. 请到「账号池」页面上传 token_file "
                "(必填) + cookie_file (可选).",
                "auth": auth,
            }),
            400,
        )

    job_id = uuid.uuid4().hex[:12]
    params = {
        "prompt": prompt,
        "options": {
            "shot_count": options.get("shot_count"),
            "duration_sec": options.get("duration_sec") or vp.DEFAULT_SHOT_DURATION,
            "voice": options.get("voice") or vp.DEFAULT_VOICE,
            "aspect_ratio": options.get("aspect_ratio") or vp.DEFAULT_ASPECT_RATIO,
            "video_model": options.get("video_model") or "",
            "video_model_version": options.get("video_model_version") or "",
            "video_size": options.get("video_size") or "",
            "generate_audio": bool(options.get("generate_audio", True)),
            "use_llm": bool(options.get("use_llm", False)),
            "llm_model": options.get("llm_model") or "",
        },
    }
    job = db.create_job({
        "id": job_id,
        "kind": "video_pipeline",
        "status": "queued",
        "message": "排队中",
        "progress": 0,
        "prompt": prompt,
        "model": "",
        "model_version": "",
        "params": params,
    })
    db.add_log(
        job_id=job_id, phase="api_video_generate",
        method="POST", url="/api/video/generate",
        status_code=202, request_body=params,
    )
    if not _enqueue_job(job_id, "video_pipeline"):
        db.update_job(
            job_id,
            status="failed",
            message="任务队列已满，请稍后重试。",
            progress=100,
            finished_at=time.time(),
        )
        return jsonify({"error": "任务队列已满，请稍后重试。", "job_id": job_id}), 429
    return jsonify({"job_id": job_id, "job": _public_job(job)}), 202

@app.get("/api/video/<job_id>")
def api_video_get(job_id: str):
    """查 video_pipeline 任务；最终返回 final_video_path / shots / manifest。"""
    job = db.get_job(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    pub = _public_job(job) or {}
    result = job.get("result") or {}
    pub["final_video_path"] = _output_url(result.get("final_video_path"))
    pub["manifest_path"] = _output_url(result.get("manifest_path"))
    pub["shots"] = result.get("shots") or []
    pub["used_ffmpeg"] = bool(result.get("used_ffmpeg"))
    pub["ffprobe_duration_total"] = result.get("ffprobe_duration_total") or 0
    return jsonify({"job": pub})

def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    recovered = _recover_orphaned_jobs()
    learned_specs = _backfill_model_capabilities_from_logs()
    pool = get_pool()
    pool_summary = pool.status()
    try:
        by_kind, source, err = _get_models_by_kind(force_live=False)
        total = sum(len(v) for v in by_kind.values())
        print(f"[models] source={source} total={total} err={err or '-'}")
    except Exception as e:
        print(f"[models] preload failed: {e}")

    # CORS=* 在公网部署等于裸奔：警告
    cors_warn = ""
    if _cors_origins.strip() in ("", "*"):
        cors_warn = "  ⚠ CORS_ORIGINS=* 允许任意源跨域（公网部署请改成具体前端域名）"
    if os.environ.get("FLASK_PUBLIC") == "1":
        cors_warn += "\n  ⚠ FLASK_PUBLIC=1：确认已通过 Tailscale / SSH Tunnel / CF Access 保护"

    host = fp.env("FLASK_HOST", "0.0.0.0")
    port = int(fp.env("FLASK_PORT", "7860") or 7860)
    print(f"[Firefly API] http://{host}:{port}")
    print(f"  db={DB_PATH}")
    print(f"  recovered_orphans={recovered}")
    print(f"  learned_model_specs={learned_specs}")
    print(
        f"  accounts_pool: size={pool_summary['size']} available={pool_summary['available']}"
        + (" (池为空, 所有请求都会 400; 请到「账号池」页面上传)"
           if pool_summary["size"] == 0 else "")
    )
    print(f"  cors={_cors_origins}")
    if cors_warn:
        print(cors_warn)
    print("  frontend: cd frontend && npm run dev")
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)

if __name__ == "__main__":
    main()
