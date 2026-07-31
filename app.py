"""Adobe Firefly API 后端（前后端分离）。

- REST JSON API + CORS
- 任务 / 调用日志 → SQLite (data/firefly.db)
- 产物只记录下载 URL，不落盘
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request
from flask_cors import CORS

import firefly_pipeline as fp
from db import Database
from models_catalog import (
    IMAGE_MODELS,
    VIDEO_MODELS,
    flatten_discovery_models,
    split_by_kind,
)

APP_ROOT = Path(__file__).resolve().parent
# FIREFLY_DATA_DIR 用于部署到 Render / Fly 等带持久化磁盘的运行时
# （默认仍指向仓库根下的 data/，开发环境无变化）
DATA_DIR = Path(os.environ.get("FIREFLY_DATA_DIR") or (APP_ROOT / "data"))
OUT_DIR = APP_ROOT / "outputs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "firefly.db"

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
# 生产收紧 CORS（默认仍 * 便于本地调试）
_cors_origins = os.environ.get("CORS_ORIGINS", "*")
CORS(app, resources={r"/api/*": {"origins": _cors_origins.split(",") if _cors_origins != "*" else "*"}})

db = Database(DB_PATH)
_executor_sema = threading.Semaphore(2)
_models_cache: dict[str, Any] = {"ts": 0.0, "data": None, "error": ""}


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

    path = _latest_flat_path()
    if path:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else None
        except Exception:
            pass
    raws = sorted(
        [p for p in OUT_DIR.glob("models_*.json") if "flat" not in p.name],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not raws:
        return None
    try:
        families = json.loads(raws[0].read_text(encoding="utf-8"))
        if isinstance(families, list):
            return flatten_discovery_models(families)
    except Exception:
        return None
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


def _fetch_live_models() -> list[dict[str, Any]]:
    token, extras = fp.require_token()
    client = fp.FireflyClient(
        token,
        session=extras.get("_arp_session_id"),
        api_key=extras.get("_api_key"),
        org_id=extras.get("_org_id"),
    )
    families = client.list_models()
    flat = flatten_discovery_models(families)
    try:
        _save_flat_to_disk(flat, families)
    except Exception:
        pass
    return flat


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

    by_kind = split_by_kind(flat)
    _models_cache["ts"] = now
    _models_cache["data"] = by_kind
    _models_cache["error"] = err
    return by_kind, source, err


def _auth_status() -> dict[str, Any]:
    storage = Path(fp.env("FIREFLY_STORAGE", str(fp.DEFAULT_STORAGE)))
    token_file = Path(fp.env("FIREFLY_TOKEN_FILE", str(fp.DEFAULT_TOKEN_FILE)))
    info: dict[str, Any] = {
        "storage_exists": storage.exists(),
        "token_file_exists": token_file.exists(),
        "token_ok": False,
        "client_id": "",
        "expires_at": None,
        "expires_in_sec": None,
        "can_ims_refresh": storage.exists(),
    }
    tok = None
    if token_file.exists():
        try:
            import json

            data = json.loads(token_file.read_text(encoding="utf-8"))
            tok = data.get("token")
            exp = data.get("expires_at")
            info["expires_at"] = exp
            if exp:
                info["expires_in_sec"] = int(float(exp) - time.time())
                info["token_ok"] = bool(tok) and time.time() < float(exp)
            else:
                info["token_ok"] = bool(tok)
        except Exception as e:
            info["error"] = str(e)
    if tok:
        claims = fp.decode_jwt_payload(str(tok))
        info["client_id"] = claims.get("client_id") or ""
    return info


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


# ── job runner ───────────────────────────────────────────────


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

            if kind == "image":
                size = str(params.get("size") or "auto")
                detail = int(params.get("detail_level") or 3)
                db.update_job(job_id, message="生成图片中…", progress=15)
                data = fp.generate_image(
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
            else:
                duration = params.get("duration")
                duration_i = int(duration) if duration not in (None, "", 0) else None
                aspect = str(params.get("aspect_ratio") or "16:9").strip() or "16:9"
                size = params.get("size")
                if not size:
                    size = dict(
                        fp.VIDEO_SIZE_BY_ASPECT.get(aspect)
                        or fp.VIDEO_SIZE_BY_ASPECT["16:9"]
                    )
                audio = bool(params.get("generate_audio", True))
                neg = str(params.get("negative_prompt") or "").strip()
                db.update_job(job_id, message="生成视频中…", progress=15)
                data = fp.generate_video(
                    prompt,
                    model=model,
                    model_version=version,
                    n=n,
                    seeds=seeds,
                    duration=duration_i,
                    size=size,
                    aspect_ratio=aspect,
                    generate_audio=audio,
                    negative_prompt=neg,
                    poll_interval=float(params.get("poll_interval") or 6),
                    max_wait=float(params.get("max_wait") or 1800),
                    download_dir=None,
                    on_submitted=on_submitted,
                )

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
            db.update_job(
                job_id,
                status="failed",
                message=err_msg[:500],
                error=err_msg[:1000],
                traceback=tb,
                progress=100,
                finished_at=time.time(),
            )
            # ── (3b) 创建失败：异常原因 ─────────────────────────
            # 如果上游给过 task_id 仍失败，附带上下文
            failure_context = {
                "exception": err_msg,
                "exception_type": type(e).__name__,
                "submitted": submitted if submitted.get("ok") else {},
            }
            db.add_log(
                job_id=job_id,
                phase="task_failed",
                method="INTERNAL",
                url=f"generate/{kind}",
                status_code=500,
                error=err_msg,
                response_body=failure_context,
                duration_ms=(time.time() - t0) * 1000,
            )
            # 失败时也落一份完整 traceback 到单独日志行，便于排错
            db.add_log(
                job_id=job_id,
                phase="task_traceback",
                method="INTERNAL",
                url=f"generate/{kind}",
                status_code=500,
                response_body=tb[-4000:],
                duration_ms=(time.time() - t0) * 1000,
            )


# ── routes ───────────────────────────────────────────────────


@app.get("/api/health")
def api_health():
    return jsonify(
        {
            "ok": True,
            "auth": _auth_status(),
            "time": time.time(),
            "db": str(DB_PATH),
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

    auth = _auth_status()
    if not auth.get("storage_exists") and not auth.get("token_ok"):
        return (
            jsonify(
                {
                    "error": "未登录。请先运行: python token_daemon.py --start",
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
        "size": body.get("size") or ("auto" if kind == "image" else ""),
        "detail_level": body.get("detail_level", 3),
        "duration": body.get("duration"),
        "aspect_ratio": body.get("aspect_ratio") or "",
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
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return jsonify({"job_id": job_id, "job": _public_job(job)}), 202


@app.get("/api/jobs")
def api_jobs():
    limit = int(request.args.get("limit") or 50)
    offset = int(request.args.get("offset") or 0)
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
    ok = db.delete_job(job_id)
    if not ok:
        return jsonify({"error": "job not found"}), 404
    return jsonify({"ok": True})


@app.get("/api/logs")
def api_logs():
    job_id = request.args.get("job_id") or None
    limit = int(request.args.get("limit") or 100)
    offset = int(request.args.get("offset") or 0)
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


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        by_kind, source, err = _get_models_by_kind(force_live=False)
        total = sum(len(v) for v in by_kind.values())
        print(f"[models] source={source} total={total} err={err or '-'}")
    except Exception as e:
        print(f"[models] preload failed: {e}")

    host = fp.env("FLASK_HOST", "127.0.0.1")
    port = int(fp.env("FLASK_PORT", "7860") or 7860)
    print(f"[Firefly API] http://{host}:{port}")
    print(f"  db={DB_PATH}")
    print(f"  cors=*  frontend: cd frontend && npm run dev")
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
