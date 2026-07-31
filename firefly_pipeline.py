"""
Adobe Firefly 3P 流水线:
  异步提交图片 / 视频生成 → 轮询结果 → 下载产物到本地

用法:
  python firefly_pipeline.py --list
  python firefly_pipeline.py image "kimi k3 对 Claude fable5 的影响"
  python firefly_pipeline.py image "一只在咖啡馆打盹的橘猫" --n 4 --size 1024x1024
  python firefly_pipeline.py video "宇航员在火星上漫步，远处是日落" --duration 8
  python firefly_pipeline.py batch prompts.txt --kind image --n 1

环境变量:
  FIREFLY_BASE_URL     默认 https://firefly-3p.ff.adobe.io
  FIREFLY_TOKEN        Bearer Token（或从 current_token.json 读）
  FIREFLY_API_KEY      默认跟 JWT client_id（clio-playground-web / projectx_webapp）
  FIREFLY_ORIGIN       默认按 client_id 选 firefly.adobe.com 或 new.express.adobe.com
  FIREFLY_SESSION      x-arp-session-id；不设则自动生成 base64({sid,ftr})
  FIREFLY_JOBS_HOST    默认 bks-epo8552.adobe.io
  FIREFLY_MODEL_IMAGE  默认 gpt-image
  FIREFLY_MODEL_VIDEO  默认 gpt-video
  FIREFLY_IMPERSONATE  默认 chrome124（需 curl_cffi）
  FIREFLY_IMS_REFRESH  默认 1；有 storage.json 时用 cookie 走 IMS check/v6 刷 token
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

APP_ROOT = Path(__file__).resolve().parent
_DATA_DIR_ENV = os.environ.get("FIREFLY_DATA_DIR")
if _DATA_DIR_ENV:
    DATA_DIR = Path(_DATA_DIR_ENV)
else:
    DATA_DIR = APP_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR = APP_ROOT / "outputs"
DEFAULT_STORAGE = DATA_DIR / "storage.json"
DEFAULT_TOKEN_FILE = DATA_DIR / "current_token.json"

DEFAULT_BASE = "https://firefly-3p.ff.adobe.io"
DEFAULT_JOBS_HOST = "bks-epo8552.adobe.io"
DEFAULT_MODEL_IMAGE = "gpt-image"
DEFAULT_MODEL_VIDEO = "gpt-video"
# Express / adobe2api
API_KEY_PROJECTX = "projectx_webapp"
ORIGIN_EXPRESS = "https://new.express.adobe.com"
# firefly.adobe.com playground（token daemon 抓到的就是这个）
API_KEY_CLIO = "clio-playground-web"
ORIGIN_FIREFLY = "https://firefly.adobe.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
DEFAULT_SEC_CH_UA = (
    '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"'
)
DEFAULT_IMPERSONATE = "chrome124"
IMS_CHECK_URL = (
    "https://adobeid-na1.services.adobe.com/ims/check/v6/token"
    "?jslVersion=v2-v0.48.0-1-g1e322cb"
)
IMS_SCOPE_NARROW = "AdobeID,firefly_api,openid"

# client_id → (默认 origin, generate 是否必须带 arp)
CLIENT_PROFILES: dict[str, tuple[str, bool]] = {
    API_KEY_PROJECTX: (ORIGIN_EXPRESS, False),  # express: arp 可选
    API_KEY_CLIO: (ORIGIN_FIREFLY, True),  # firefly playground: arp 必需，否则 408
}


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def base_url() -> str:
    return env("FIREFLY_BASE_URL", DEFAULT_BASE).rstrip("/")


def jobs_host() -> str:
    return env("FIREFLY_JOBS_HOST", DEFAULT_JOBS_HOST)


def decode_jwt_payload(token: str) -> dict[str, Any]:
    raw = str(token or "").strip()
    parts = raw.split(".")
    if len(parts) < 2:
        return {}
    payload_part = parts[1]
    pad = "=" * (-len(payload_part) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload_part + pad))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def profile_for_token(token: str) -> tuple[str, str, bool]:
    """返回 (api_key, origin, arp_required)。"""
    claims = decode_jwt_payload(token)
    cid = str(claims.get("client_id") or "").strip()
    env_key = env("FIREFLY_API_KEY") or env("ADOBE_API_KEY")
    api_key = env_key or cid or API_KEY_CLIO
    origin_default, arp_required = CLIENT_PROFILES.get(
        api_key, (ORIGIN_FIREFLY, True)
    )
    origin = env("FIREFLY_ORIGIN") or origin_default
    # clio 强制 arp；projectx 默认不强制但带上也无
    if api_key == API_KEY_CLIO:
        arp_required = True
    return api_key, origin.rstrip("/"), arp_required


def generate_arp_session_id() -> str:
    """x-arp-session-id = base64(JSON{sid,ftr})。

    实测: clio-playground-web token 不带此头 → 稳定 408 system under load。
    UUID 格式无效；必须是可 JSON parse 的 base64。
    """
    now_ms = int(time.time() * 1000)
    ftr = f"{os.urandom(16).hex()}_{now_ms}_{os.getpid()}_dUAL43-mnts-ants-d4_31ck__tt"
    raw = json.dumps(
        {"sid": str(uuid.uuid4()), "ftr": ftr},
        separators=(",", ":"),
    )
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _is_valid_arp(value: str | None) -> bool:
    """拒绝 UUID；接受 base64({sid,...})。"""
    v = str(value or "").strip()
    if not v or len(v) < 20:
        return False
    # UUID v4 形态直接否
    if len(v) == 36 and v.count("-") == 4:
        return False
    try:
        pad = "=" * (-len(v) % 4)
        data = json.loads(base64.b64decode(v + pad))
        return isinstance(data, dict) and bool(data.get("sid"))
    except Exception:
        # 有些真实 arp 可能是其它编码；非 UUID 的长串仍尝试使用
        return len(v) > 40 and not v.count(" ")


def resolve_arp_session_id(preferred: str | None = None) -> str:
    for cand in (env("FIREFLY_SESSION"), preferred):
        if _is_valid_arp(cand):
            return str(cand).strip()
    return generate_arp_session_id()


def _cookie_header_from_storage(storage_path: Path) -> str:
    try:
        storage = json.loads(storage_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    seen: set[str] = set()
    parts: list[str] = []
    for c in storage.get("cookies") or []:
        name = c.get("name")
        value = c.get("value")
        if not name or value is None or name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def ims_refresh_token(
    *,
    client_id: str = API_KEY_PROJECTX,
    origin: str = ORIGIN_EXPRESS,
    storage_path: Path | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """用 storage cookie 调 IMS check/v6/token（adobe2api 同款）。"""
    sp = storage_path or Path(env("FIREFLY_STORAGE", str(DEFAULT_STORAGE)))
    if not sp.exists():
        return None
    cookie = _cookie_header_from_storage(sp)
    if "ims_sid=" not in cookie and "aux_sid=" not in cookie:
        return None
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Cookie": cookie,
        "Origin": origin,
        "Referer": f"{origin}/",
        "User-Agent": env("FIREFLY_USER_AGENT", DEFAULT_USER_AGENT),
    }
    form = {
        "client_id": client_id,
        "guest_allowed": "true",
        "scope": IMS_SCOPE_NARROW,
    }
    try:
        try:
            from curl_cffi.requests import Session as CurlSession

            with CurlSession(impersonate=env("FIREFLY_IMPERSONATE", DEFAULT_IMPERSONATE), timeout=30) as sess:
                r = sess.post(IMS_CHECK_URL, headers=headers, data=form)
        except ImportError:
            r = requests.post(IMS_CHECK_URL, headers=headers, data=form, timeout=30)
    except Exception as e:
        print(f"[警告] IMS 刷新网络失败: {e}", file=sys.stderr)
        return None
    if r.status_code != 200:
        print(
            f"[警告] IMS 刷新 HTTP {r.status_code}: {(r.text or '')[:160]}",
            file=sys.stderr,
        )
        return None
    try:
        data = r.json()
    except Exception:
        return None
    tok = str(data.get("access_token") or data.get("token") or "").strip()
    if not tok:
        return None
    return tok, data


def require_token() -> tuple[str, dict]:
    """返回 (token, extras)。

    绕过 408 实测结论 (2026-07):
      - firefly daemon 的 token client_id=clio-playground-web
      - 必须: x-api-key=clio-playground-web + origin=firefly.adobe.com
             + x-arp-session-id=base64({sid,ftr})
      - 缺 arp → 稳定 408；UUID arp 无效
      - projectx_webapp token (IMS cookie 刷) + express origin: arp 可选
      - generate 不要带 Cookie（带了反而 431）
    """
    extras: dict[str, str] = {}

    # 可选: 优先 IMS cookie 刷 projectx（与 adobe2api 一致，arp 可选）
    if env("FIREFLY_IMS_REFRESH", "1") in ("1", "true", "yes"):
        prefer = env("FIREFLY_IMS_CLIENT", API_KEY_CLIO)  # 默认跟 firefly 站
        origin = (
            ORIGIN_EXPRESS if prefer == API_KEY_PROJECTX else ORIGIN_FIREFLY
        )
        refreshed = ims_refresh_token(client_id=prefer, origin=origin)
        if refreshed:
            tok, meta = refreshed
            claims = decode_jwt_payload(tok)
            print(
                f"[信息] IMS cookie 刷新成功 client_id={claims.get('client_id')}"
            )
            extras["_api_key"] = str(claims.get("client_id") or prefer)
            extras["_from_ims"] = "1"
            # 写回 token 文件，供下次/其它进程使用
            tf = Path(env("FIREFLY_TOKEN_FILE", str(DEFAULT_TOKEN_FILE)))
            try:
                exp_in = int(meta.get("expires_in") or 86000)
                payload = {
                    "token": tok,
                    "expires_in": exp_in,
                    "expires_at": time.time() + exp_in,
                    "captured_at": time.time(),
                    "client_id": claims.get("client_id"),
                    "source": "ims_check_v6",
                }
                tf.parent.mkdir(parents=True, exist_ok=True)
                tmp = tf.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                tmp.replace(tf)
            except Exception as e:
                print(f"[警告] 写 token 文件失败: {e}", file=sys.stderr)
            return tok, extras

    t = env("FIREFLY_TOKEN")
    if t:
        return t, extras

    candidates = [
        Path(env("FIREFLY_TOKEN_FILE", str(DEFAULT_TOKEN_FILE))),
        Path(env("FIREFLY_OAUTH_TOKEN_FILE", str(DATA_DIR / "oauth_token.json"))),
    ]
    for tf in candidates:
        if not tf.exists():
            continue
        try:
            data = json.loads(tf.read_text(encoding="utf-8"))
            tok = data.get("token") or data.get("access_token") or data.get("value")
            exp = data.get("expires_at", 0)
            if not tok:
                continue
            if exp and time.time() > exp:
                print(
                    f"[警告] {tf.name} 已过期 ({datetime.fromtimestamp(exp).isoformat()})",
                    file=sys.stderr,
                )
                continue
            print(f"[信息] 从 {tf.name} 读取 token")
            claims = decode_jwt_payload(str(tok))
            cid = str(
                data.get("client_id") or claims.get("client_id") or ""
            ).strip()
            if cid:
                extras["_api_key"] = cid
            org = data.get("org_id")
            if org and not env("FIREFLY_ORG_ID"):
                extras["_org_id"] = org
            arp = data.get("arp_session_id") or (
                (data.get("headers") or {}).get("x-arp-session-id")
                if isinstance(data.get("headers"), dict)
                else None
            )
            if _is_valid_arp(arp):
                extras["_arp_session_id"] = str(arp).strip()
            return str(tok), extras
        except Exception as e:
            print(f"[警告] 读 {tf} 失败: {e}", file=sys.stderr)

    raise RuntimeError(
        "未设置 FIREFLY_TOKEN, 且未找到可用的 token 文件。"
        "先跑 python token_daemon.py --start, 或保证 data/storage.json 有 ims_sid。"
    )


def require_token_simple() -> str:
    t, _ = require_token()
    return t


def browser_headers(origin: str | None = None) -> dict[str, str]:
    origin = (origin or env("FIREFLY_ORIGIN") or ORIGIN_FIREFLY).rstrip("/")
    ua = env("FIREFLY_USER_AGENT", DEFAULT_USER_AGENT)
    sec_ch_ua = env("FIREFLY_SEC_CH_UA", DEFAULT_SEC_CH_UA)
    return {
        "user-agent": ua,
        "origin": origin,
        "referer": f"{origin}/",
        "accept-language": "en-US,en;q=0.9",
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-site": "cross-site",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }


def safe_name(text: str, limit: int = 48) -> str:
    import re

    s = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", text.strip())[:limit].strip("_")
    return s or "output"


def new_seed() -> int:
    return random.randint(1, 2_147_483_647)


def parse_size(size: Any) -> dict[str, int] | None:
    """把 size 规范成上游要求的对象 {"width": int, "height": int}。

    接受:
      - {"width": 854, "height": 480}
      - "854x480" / "854X480"
      - (854, 480)
    """
    if size is None or size == "" or size == "auto":
        return None
    if isinstance(size, dict):
        w, h = size.get("width"), size.get("height")
        if w is not None and h is not None:
            return {"width": int(w), "height": int(h)}
        return None
    if isinstance(size, (list, tuple)) and len(size) >= 2:
        return {"width": int(size[0]), "height": int(size[1])}
    if isinstance(size, str):
        s = size.strip().lower().replace("*", "x").replace("×", "x")
        if "x" in s:
            a, b = s.split("x", 1)
            try:
                return {"width": int(a.strip()), "height": int(b.strip())}
            except ValueError:
                return None
    return None


# 视频默认分辨率（按比例）
VIDEO_SIZE_BY_ASPECT: dict[str, dict[str, int]] = {
    "16:9": {"width": 854, "height": 480},
    "9:16": {"width": 480, "height": 854},
    "1:1": {"width": 720, "height": 720},
    "4:3": {"width": 640, "height": 480},
    "3:4": {"width": 480, "height": 640},
    "21:9": {"width": 1280, "height": 548},
}


class FireflyClient:
    def __init__(
        self,
        token: str,
        session: str | None = None,
        api_key: str | None = None,
        org_id: str | None = None,
        origin: str | None = None,
    ) -> None:
        self.token = token
        auto_key, auto_origin, arp_required = profile_for_token(token)
        self.api_key = (
            api_key
            or env("FIREFLY_API_KEY")
            or env("ADOBE_API_KEY")
            or auto_key
        )
        self.origin = (origin or env("FIREFLY_ORIGIN") or auto_origin).rstrip("/")
        self.arp_required = arp_required or (self.api_key == API_KEY_CLIO)
        # clio 必须有效 arp；projectx 也带上无害
        self.session = resolve_arp_session_id(session)
        self.org_id = org_id or env("FIREFLY_ORG_ID") or None
        self.base = base_url()
        self.jobs_host = jobs_host()
        self.impersonate = env("FIREFLY_IMPERSONATE", DEFAULT_IMPERSONATE)
        try:
            from curl_cffi.requests import Session as CurlSession

            self.session_http = CurlSession(impersonate=self.impersonate, timeout=60)
            self._using_curl = True
        except ImportError:
            print(
                "[警告] 未安装 curl_cffi, TLS 指纹像脚本, 易 408。"
                "建议: pip install curl_cffi",
                file=sys.stderr,
            )
            self.session_http = requests.Session()
            self._using_curl = False
        # generate 不要加载 cookie（实测带 Cookie → 431）

    def _headers(self) -> dict[str, str]:
        h = browser_headers(self.origin)
        h.update(
            {
                "Authorization": f"Bearer {self.token}",
                "content-type": "application/json",
                "accept": "*/*",
                "x-api-key": self.api_key,
                # clio 缺此头 → 408；projectx 带上也 OK
                "x-arp-session-id": self.session or generate_arp_session_id(),
            }
        )
        if self.org_id:
            h["x-gw-ims-orgid"] = self.org_id
        return h

    def _post_with_retry(
        self,
        url: str,
        body: dict[str, Any],
        *,
        timeout: int = 60,
        max_retries: int = 5,
        base_delay: float = 3.0,
        label: str = "POST",
    ) -> requests.Response:
        """POST + 指数退避重试。

        对齐 adobe2api: 默认重试 429/451/5xx。
        408 在 adobe2api 里不当瞬时错误 (多半是指纹/api-key 错);
        这里仍短暂重试 1~2 次, 若持续 408 给出诊断提示。
        """
        # adobe2api retry_on_status_codes = [429, 451, 500, 502, 503, 504]
        # 408 单独限次, 避免把「配置错误」当成「负载」无限重试
        soft_retryable = {429, 451, 500, 502, 503, 504}
        last_err: Exception | None = None
        last_408_body = ""
        for attempt in range(max_retries + 1):
            try:
                r = self.session_http.post(
                    url, headers=self._headers(), json=body, timeout=timeout
                )
            except (requests.ConnectionError, requests.Timeout) as e:
                last_err = e
                if attempt < max_retries:
                    delay = base_delay * (2**attempt)
                    print(
                        f"[重试 {attempt + 1}/{max_retries}] {label} 网络错误: {e}, "
                        f"{delay:.0f}s 后重试"
                    )
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"{label} 网络失败: {e}") from e

            if r.status_code < 400:
                return r

            # 401/403: 额度耗尽 / token 失效, 不重试
            if r.status_code in (401, 403):
                access_err = r.headers.get("x-access-error") or ""
                if access_err == "taste_exhausted":
                    raise RuntimeError(
                        f"{label} 额度耗尽 (x-access-error=taste_exhausted)"
                    )
                raise RuntimeError(
                    f"{label} 鉴权失败 HTTP {r.status_code}"
                    f"{f' ({access_err})' if access_err else ''}: {r.text[:300]}"
                )

            is_408 = r.status_code == 408
            is_soft = r.status_code in soft_retryable
            # 408 最多再试 2 次 (真实瞬时负载); 持续 408 = 指纹/key 问题
            can_retry_408 = is_408 and attempt < min(2, max_retries)
            can_retry = (is_soft and attempt < max_retries) or can_retry_408

            if can_retry:
                delay = base_delay * (2**attempt)
                try:
                    err_body = r.json()
                    msg = (
                        err_body.get("message")
                        or err_body.get("error")
                        or err_body.get("title")
                        or r.text[:100]
                    )
                except Exception:
                    msg = r.text[:100]
                if is_408:
                    last_408_body = msg
                print(
                    f"[重试 {attempt + 1}/{max_retries}] {label} HTTP {r.status_code} "
                    f"({msg}), {delay:.0f}s 后重试"
                )
                time.sleep(delay)
                continue

            if is_408:
                hint = (
                    "持续 408 不是真负载。clio-playground-web token 必须带 "
                    "x-arp-session-id=base64({sid,ftr})，且 x-api-key/origin 与 JWT client_id 一致。"
                    " projectx_webapp 用 origin=new.express.adobe.com；"
                    " clio 用 origin=https://firefly.adobe.com。不要把 Cookie 带到 generate。"
                )
                raise RuntimeError(
                    f"{label} HTTP 408: {last_408_body or r.text[:300]}\n[提示] {hint}"
                )

            raise RuntimeError(f"{label} HTTP {r.status_code}: {r.text[:500]}")

        raise RuntimeError(f"{label} 重试 {max_retries} 次后仍失败: {last_err}")

    def list_models(self, include_first_party: bool = True) -> list[dict[str, Any]]:
        print("[1/4] 查询模型清单...")
        url = f"{self.base}/v2/models/discovery"
        body = {
            "filters": {
                "resolveSchema": True,
                "includeFirstParty": include_first_party,
            }
        }
        r = self._post_with_retry(url, body, timeout=60, label="models/discovery")
        data = r.json()
        models = (
            data.get("models")
            or data.get("data")
            or (data.get("result") or {}).get("models")
            or []
        )
        if not isinstance(models, list):
            raise RuntimeError(f"模型清单格式异常: {json.dumps(data, ensure_ascii=False)[:400]}")
        print(f"[OK] 模型数量: {len(models)}")
        return models

    def submit_image(
        self,
        prompt: str,
        *,
        model: str,
        model_version: str = "2",
        n: int = 1,
        size: str = "auto",
        seeds: list[int] | None = None,
        detail_level: int = 3,
        reference_blobs: list[str] | None = None,
        store_inputs: bool = True,
        timeout: int = 60,
    ) -> tuple[str, str]:
        """返回 (task_id, poll_url)。poll_url 可能来自 x-override-status-link。"""
        url = f"{self.base}/v2/3p-images/generate-async"
        body: dict[str, Any] = {
            "n": n,
            "seeds": seeds if seeds is not None else [new_seed() for _ in range(n)],
            "output": {"storeInputs": store_inputs},
            "prompt": prompt,
            "referenceBlobs": reference_blobs or [],
            "modelSpecificPayload": {"size": size},
            "modelId": model,
            "modelVersion": model_version,
            "generationMetadata": {
                "module": "text2image",
                "submodule": "ff-image-generate",
            },
            "generationSettings": {"detailLevel": detail_level},
        }
        r = self._post_with_retry(url, body, timeout=timeout, label="图片提交")
        # 缓存上游响应，便于上层记录日志（status code + json body）
        try:
            self._last_submit_response = r
            try:
                self._last_submit_response._body_dict = r.json()
            except Exception:
                self._last_submit_response._body_dict = None
        except Exception:
            pass
        return self._extract_task_and_poll(r, kind="image")

    def submit_video(
        self,
        prompt: str,
        *,
        model: str,
        model_version: str = "1",
        n: int = 1,
        seeds: list[int] | None = None,
        reference_blobs: list[str] | None = None,
        store_inputs: bool = True,
        duration: int | None = None,
        size: Any = None,
        aspect_ratio: str = "",
        generate_audio: bool = True,
        negative_prompt: str = "",
        timeout: int = 60,
    ) -> tuple[str, str]:
        """提交视频。body 对齐浏览器/成功抓包:

        size 必须是 {"width": int, "height": int}，不能是 "854x480" 字符串。
        """
        url = f"{self.base}/v2/3p-videos/generate-async"
        seed_list = seeds if seeds is not None else [new_seed() for _ in range(max(1, n))]
        aspect = (aspect_ratio or "16:9").strip()

        size_obj = parse_size(size)
        if size_obj is None:
            size_obj = dict(VIDEO_SIZE_BY_ASPECT.get(aspect) or VIDEO_SIZE_BY_ASPECT["16:9"])

        # 与成功请求一致的最小字段集（seedance / veo / kling 通用）
        body: dict[str, Any] = {
            "modelId": model,
            "modelVersion": model_version,
            "size": size_obj,
            "seeds": seed_list,
            "prompt": prompt,
            "generateAudio": bool(generate_audio),
            "generationMetadata": {
                "module": "text2video",
                "submodule": "ff-video-generate",
            },
            "generationSettings": {"aspectRatio": aspect},
            "output": {"storeInputs": store_inputs},
        }
        if duration:
            body["duration"] = int(duration)
        if negative_prompt:
            body["negativePrompt"] = negative_prompt
        if reference_blobs:
            body["referenceBlobs"] = reference_blobs
        # 部分模型仍认 n
        if n and n > 1:
            body["n"] = int(n)

        print(
            f"[信息] video body model={model}:{model_version} "
            f"size={size_obj} duration={duration} aspect={aspect} audio={generate_audio}"
        )
        r = self._post_with_retry(url, body, timeout=timeout, label="视频提交")
        try:
            self._last_submit_response = r
            try:
                self._last_submit_response._body_dict = r.json()
            except Exception:
                self._last_submit_response._body_dict = None
        except Exception:
            pass
        return self._extract_task_and_poll(r, kind="video")

    def _extract_task_and_poll(
        self, resp: Any, *, kind: str
    ) -> tuple[str, str]:
        """对齐 adobe2api: 优先 x-override-status-link / links.result。"""
        try:
            data = resp.json()
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}

        poll_url = str(getattr(resp, "headers", {}).get("x-override-status-link") or "").strip()
        if not poll_url:
            links = data.get("links") if isinstance(data.get("links"), dict) else {}
            result_link = links.get("result")
            if isinstance(result_link, str):
                poll_url = result_link.strip()
            elif isinstance(result_link, dict):
                poll_url = str(result_link.get("href") or "").strip()

        if poll_url:
            poll_url = self._normalize_poll_url(poll_url)

        task_id = ""
        for k in ("taskId", "task_id", "jobId", "job_id", "id", "requestId", "request_id"):
            v = data.get(k)
            if v:
                task_id = str(v)
                break
        if not task_id:
            nested = data.get("result") or data.get("data") or {}
            if isinstance(nested, dict):
                for k in ("taskId", "task_id", "jobId", "id"):
                    v = nested.get(k)
                    if v:
                        task_id = str(v)
                        break
        if not task_id and poll_url:
            parts = [p for p in urlparse(poll_url).path.split("/") if p]
            if parts:
                task_id = parts[-1]
        if not task_id:
            raise RuntimeError(
                f"{kind} 未返回 taskId: {json.dumps(data, ensure_ascii=False)[:500]}"
            )
        print(f"[OK] {kind} taskId={task_id}")
        if poll_url:
            print(f"[OK] {kind} poll={poll_url[:100]}")
        return task_id, poll_url

    @staticmethod
    def _normalize_poll_url(raw_url: str) -> str:
        """对齐 adobe2api._normalize_video_poll_url: firefly-epoXXXX → bks-epoXXXX。"""
        if not raw_url:
            return raw_url
        try:
            parsed = urlparse(raw_url)
            host = parsed.netloc
            path_parts = [p for p in parsed.path.split("/") if p]
            if not host or not path_parts:
                return raw_url
            if not host.startswith("firefly-epo"):
                return raw_url
            job_id = path_parts[-1]
            if not job_id:
                return raw_url
            host_suffix = host[len("firefly-epo") :].split(".", 1)[0]
            shard = host_suffix[:4].strip()
            if len(shard) != 4 or not shard.isdigit():
                return raw_url
            return f"https://bks-epo{shard}.adobe.io/v2/jobs/result/{job_id}?host={host}/"
        except Exception:
            return raw_url

    def poll(
        self,
        task_id: str,
        *,
        poll_url: str = "",
        interval: float = 4.0,
        max_wait: float = 900.0,
        timeout: int = 60,
    ) -> dict[str, Any]:
        print(f"[2/4] 轮询任务 {task_id}（每 {interval}s，最多 {int(max_wait)}s）...")
        if poll_url:
            url = poll_url
            params = None
        else:
            url = f"https://{self.jobs_host}/v2/jobs/result/{task_id}"
            params = {
                "host": f"{urlparse(self.base).hostname or 'firefly-3p.ff.adobe.io'}"
            }
        start = time.time()
        last = ""
        while True:
            elapsed = time.time() - start
            if elapsed > max_wait:
                raise TimeoutError(f"任务超时 {max_wait}s")
            r = self.session_http.get(
                url, headers=self._headers(), params=params, timeout=timeout
            )
            if r.status_code in (401, 403):
                raise RuntimeError(f"查询鉴权失败 HTTP {r.status_code}: {r.text[:300]}")
            if r.status_code >= 400:
                raise RuntimeError(f"查询失败 HTTP {r.status_code}: {r.text[:300]}")
            try:
                data = r.json()
            except ValueError:
                data = {"raw": r.text[:400]}
            status_header = str(r.headers.get("x-task-status") or "").lower()
            status = self._status_of(data) or status_header
            progress = (
                data.get("progress")
                or (data.get("result") or {}).get("progress")
                or r.headers.get("x-task-progress")
                or ""
            )
            line = f"status={status} progress={progress} ({int(elapsed)}s)"
            if line != last:
                print(f"      {line}")
                last = line
            # adobe2api: 有 outputs 即完成
            outputs = data.get("outputs") if isinstance(data, dict) else None
            if outputs:
                return data
            if self._is_done(status):
                return data
            if self._is_failed(status):
                raise RuntimeError(f"任务失败: {json.dumps(data, ensure_ascii=False)[:600]}")
            time.sleep(interval)

    @staticmethod
    def _status_of(data: dict[str, Any]) -> str:
        for k in ("status", "state", "jobStatus"):
            v = data.get(k)
            if v:
                return str(v).lower()
        r = data.get("result") or {}
        if isinstance(r, dict):
            for k in ("status", "state"):
                v = r.get(k)
                if v:
                    return str(v).lower()
        return ""

    @staticmethod
    def _is_done(status: str) -> bool:
        return status in ("succeeded", "success", "completed", "complete", "done", "finished")

    @staticmethod
    def _is_failed(status: str) -> bool:
        return status in ("failed", "error", "expired", "cancelled", "canceled", "rejected")

    def collect_outputs(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        urls: list[str] = []
        blobs: list[str] = []
        seeds: list[int] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    kl = str(k).lower()
                    if kl in ("url", "imageurl", "videourl", "outputurl", "presignedurl", "downloadurl"):
                        if isinstance(v, str) and v.startswith(("http://", "https://")):
                            urls.append(v)
                    elif kl in ("blob", "blobid", "blob_id", "id") and isinstance(v, str) and 8 < len(v) < 200 and not v.startswith("http"):
                        blobs.append(v)
                    elif kl == "seeds" and isinstance(v, list):
                        for s in v:
                            if isinstance(s, (int, float)):
                                seeds.append(int(s))
                    else:
                        walk(v)
            elif isinstance(node, list):
                for it in node:
                    walk(it)

        walk(data)
        seen = set()
        uniq_urls = [u for u in urls if not (u in seen or seen.add(u))]
        seen_b = set()
        uniq_blobs = [b for b in blobs if not (b in seen_b or seen_b.add(b))]
        if not uniq_urls and not uniq_blobs:
            raise RuntimeError(
                f"完成但未解析到产物: {json.dumps(data, ensure_ascii=False)[:600]}"
            )
        return [{"url": u} for u in uniq_urls] + [{"blob": b} for b in uniq_blobs]


def download(url: str, path: Path, timeout: int = 300) -> Path:
    print(f"[下载] {url[:90]}...")
    path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, timeout=timeout, stream=True) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                if chunk:
                    f.write(chunk)
    print(f"[保存] {path.resolve()}")
    return path


def guess_ext(url: str, default: str) -> str:
    ext = Path(urlparse(url).path).suffix.lower()
    if ext and len(ext) <= 5:
        return ext
    return default


def _make_client() -> FireflyClient:
    token, extras = require_token()
    client = FireflyClient(
        token,
        session=extras.get("_arp_session_id"),
        api_key=extras.get("_api_key"),
        org_id=extras.get("_org_id"),
    )
    print(
        f"[信息] api-key={client.api_key} origin={client.origin} "
        f"arp={'yes' if client.session else 'no'} "
        f"curl_cffi={client._using_curl} impersonate={client.impersonate}"
    )
    return client


def generate_image(
    prompt: str,
    *,
    model: str,
    model_version: str = "2",
    n: int = 1,
    size: str = "auto",
    seeds: list[int] | None = None,
    detail_level: int = 3,
    poll_interval: float = 4.0,
    max_wait: float = 900.0,
    download_dir: Path | None = None,
    on_submitted: Callable[[str, str, int, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """生成图片。默认只返回下载 URL，不落盘。

    返回:
      {
        task_id, poll_url, result, outputs:[{url|blob, type}],
        submit_status_code, submit_response,
      }
    on_submitted(task_id, poll_url, status_code, response_body) 在上游
    返回 task_id 后被调用，便于 app.py 立即写日志（不等轮询结束）。
    """
    client = _make_client()
    task_id, poll_url = client.submit_image(
        prompt,
        model=model,
        model_version=model_version,
        n=n,
        size=size,
        seeds=seeds,
        detail_level=detail_level,
    )
    if on_submitted:
        try:
            submit_resp = getattr(client, "_last_submit_response", None)
            submit_code = int(getattr(submit_resp, "status_code", 200)) if submit_resp else 200
            submit_body = getattr(submit_resp, "_body_dict", None) if submit_resp else None
            on_submitted(task_id, poll_url, submit_code, submit_body or {})
        except Exception:
            pass

    result = client.poll(
        task_id, poll_url=poll_url, interval=poll_interval, max_wait=max_wait
    )
    raw_outputs = client.collect_outputs(result)
    outputs: list[dict[str, Any]] = []
    for item in raw_outputs:
        if "url" in item:
            outputs.append(
                {
                    "type": "image",
                    "url": item["url"],
                    "ext": guess_ext(item["url"], ".jpg"),
                }
            )
        elif "blob" in item:
            outputs.append({"type": "blob", "blob": item["blob"]})
    print(f"[OK] image task={task_id} urls={sum(1 for o in outputs if o.get('url'))}")

    if download_dir is not None:
        work = Path(download_dir)
        work.mkdir(parents=True, exist_ok=True)
        (work / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        files = []
        for i, o in enumerate(outputs):
            if not o.get("url"):
                continue
            p = work / f"image_{i:02d}{o.get('ext') or '.jpg'}"
            download(o["url"], p)
            files.append(str(p.resolve()))
            o["local_path"] = str(p.resolve())
        (work / "meta.json").write_text(
            json.dumps(
                {
                    "kind": "image",
                    "prompt": prompt,
                    "model": model,
                    "model_version": model_version,
                    "task_id": task_id,
                    "outputs": outputs,
                    "files": files,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    return {
        "task_id": task_id,
        "poll_url": poll_url,
        "result": result,
        "outputs": outputs,
        "submit_status_code": int(
            getattr(getattr(client, "_last_submit_response", None), "status_code", 200)
        )
        if getattr(client, "_last_submit_response", None)
        else 200,
    }


def run_image(
    prompt: str,
    out_dir: Path,
    *,
    model: str,
    model_version: str = "2",
    n: int = 1,
    size: str = "auto",
    seeds: list[int] | None = None,
    detail_level: int = 3,
    poll_interval: float = 4.0,
    max_wait: float = 900.0,
) -> list[Path]:
    """CLI 兼容：下载到本地并返回路径列表。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work = Path(out_dir) / f"{stamp}_{safe_name(prompt)}"
    data = generate_image(
        prompt,
        model=model,
        model_version=model_version,
        n=n,
        size=size,
        seeds=seeds,
        detail_level=detail_level,
        poll_interval=poll_interval,
        max_wait=max_wait,
        download_dir=work,
    )
    paths = [
        Path(o["local_path"])
        for o in data.get("outputs") or []
        if o.get("local_path")
    ]
    print(f"[4/4] 完成，{len(paths)} 张图，目录: {work.resolve()}")
    return paths


def generate_video(
    prompt: str,
    *,
    model: str,
    model_version: str = "1",
    n: int = 1,
    seeds: list[int] | None = None,
    duration: int | None = None,
    size: Any = None,
    aspect_ratio: str = "",
    generate_audio: bool = True,
    negative_prompt: str = "",
    poll_interval: float = 6.0,
    max_wait: float = 1800.0,
    download_dir: Path | None = None,
    on_submitted: Callable[[str, str, int, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """生成视频。默认只返回下载 URL，不落盘。

    返回结构同 generate_image；on_submitted 在上游返回 task_id 后立即触发。
    """
    client = _make_client()
    task_id, poll_url = client.submit_video(
        prompt,
        model=model,
        model_version=model_version,
        n=n,
        seeds=seeds,
        duration=duration,
        size=size,
        aspect_ratio=aspect_ratio,
        generate_audio=generate_audio,
        negative_prompt=negative_prompt,
    )
    if on_submitted:
        try:
            submit_resp = getattr(client, "_last_submit_response", None)
            submit_code = int(getattr(submit_resp, "status_code", 200)) if submit_resp else 200
            submit_body = getattr(submit_resp, "_body_dict", None) if submit_resp else None
            on_submitted(task_id, poll_url, submit_code, submit_body or {})
        except Exception:
            pass

    result = client.poll(
        task_id, poll_url=poll_url, interval=poll_interval, max_wait=max_wait
    )
    raw_outputs = client.collect_outputs(result)
    outputs: list[dict[str, Any]] = []
    for item in raw_outputs:
        if "url" in item:
            outputs.append(
                {
                    "type": "video",
                    "url": item["url"],
                    "ext": guess_ext(item["url"], ".mp4"),
                }
            )
        elif "blob" in item:
            outputs.append({"type": "blob", "blob": item["blob"]})
    print(f"[OK] video task={task_id} urls={sum(1 for o in outputs if o.get('url'))}")

    if download_dir is not None:
        work = Path(download_dir)
        work.mkdir(parents=True, exist_ok=True)
        (work / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        files = []
        for i, o in enumerate(outputs):
            if not o.get("url"):
                continue
            p = work / f"video_{i:02d}{o.get('ext') or '.mp4'}"
            download(o["url"], p)
            files.append(str(p.resolve()))
            o["local_path"] = str(p.resolve())
        (work / "meta.json").write_text(
            json.dumps(
                {
                    "kind": "video",
                    "prompt": prompt,
                    "model": model,
                    "model_version": model_version,
                    "duration": duration,
                    "task_id": task_id,
                    "outputs": outputs,
                    "files": files,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    last = getattr(client, "_last_submit_response", None)
    return {
        "task_id": task_id,
        "poll_url": poll_url,
        "result": result,
        "outputs": outputs,
        "submit_status_code": int(getattr(last, "status_code", 200)) if last else 200,
    }


def run_video(
    prompt: str,
    out_dir: Path,
    *,
    model: str,
    model_version: str = "1",
    n: int = 1,
    seeds: list[int] | None = None,
    duration: int | None = None,
    size: Any = None,
    aspect_ratio: str = "",
    generate_audio: bool = True,
    negative_prompt: str = "",
    poll_interval: float = 6.0,
    max_wait: float = 1800.0,
) -> list[Path]:
    """CLI 兼容：下载到本地并返回路径列表。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work = Path(out_dir) / f"{stamp}_{safe_name(prompt)}"
    data = generate_video(
        prompt,
        model=model,
        model_version=model_version,
        n=n,
        seeds=seeds,
        duration=duration,
        size=size,
        aspect_ratio=aspect_ratio,
        generate_audio=generate_audio,
        negative_prompt=negative_prompt,
        poll_interval=poll_interval,
        max_wait=max_wait,
        download_dir=work,
    )
    paths = [
        Path(o["local_path"])
        for o in data.get("outputs") or []
        if o.get("local_path")
    ]
    print(f"[4/4] 完成，{len(paths)} 个视频，目录: {work.resolve()}")
    return paths


def run_list(out_dir: Path) -> Path:
    from models_catalog import flatten_discovery_models, split_by_kind

    token, extras = require_token()
    client = FireflyClient(
        token,
        session=extras.get("_arp_session_id"),
        api_key=extras.get("_api_key"),
        org_id=extras.get("_org_id"),
    )
    families = client.list_models()
    flat = flatten_discovery_models(families)
    by_kind = split_by_kind(flat)

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"models_{stamp}.json"
    flat_path = out_dir / f"models_flat_{stamp}.json"
    path.write_text(json.dumps(families, ensure_ascii=False, indent=2), encoding="utf-8")
    flat_path.write_text(json.dumps(flat, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[保存] 原始 family: {path.resolve()}")
    print(f"[保存] 展开 version: {flat_path.resolve()}")
    print(
        f"[摘要] family={len(families)}  version={len(flat)}  "
        f"image={len(by_kind['image'])} video={len(by_kind['video'])} "
        f"audio={len(by_kind['audio'])} other={len(by_kind['other'])}"
    )
    print()
    for kind in ("image", "video", "audio", "other"):
        items = by_kind.get(kind) or []
        if not items:
            continue
        print(f"== {kind} ({len(items)}) ==")
        for m in items:
            rel = m.get("release") or ""
            sizes = m.get("sizes") or []
            durs = m.get("durations") or []
            extra = []
            if sizes and sizes != ["auto"]:
                extra.append(f"size×{len(sizes)}")
            if durs:
                extra.append(f"dur={durs[0]}..{durs[-1]}" if len(durs) > 1 else f"dur={durs[0]}")
            if rel:
                extra.append(rel)
            tail = f"  [{', '.join(extra)}]" if extra else ""
            print(f"  - {m.get('id')}:{m.get('version')}{tail}")
        print()
    return flat_path


def run_batch(
    prompts_file: Path,
    out_dir: Path,
    *,
    kind: str,
    model: str,
    model_version: str,
    n: int,
    size: str,
) -> None:
    prompts = [ln.strip() for ln in prompts_file.read_text(encoding="utf-8").splitlines()]
    prompts = [p for p in prompts if p and not p.startswith("#")]
    if not prompts:
        raise RuntimeError(f"未在 {prompts_file} 中读到任何 prompt")
    print(f"[批量] 共 {len(prompts)} 条，kind={kind}")
    for i, p in enumerate(prompts, 1):
        print(f"\n----- [{i}/{len(prompts)}] {p[:60]} -----")
        try:
            if kind == "image":
                run_image(
                    p, out_dir,
                    model=model, model_version=model_version,
                    n=n, size=size,
                )
            else:
                run_video(
                    p, out_dir,
                    model=model, model_version=model_version,
                    n=n,
                )
        except Exception as e:
            print(f"[错误] {p[:40]} -> {e}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Adobe Firefly 3P 流水线（图片/视频异步生成 → 轮询 → 下载）"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出可用模型")
    p_list.add_argument("-o", "--out", default=str(OUT_DIR), help="输出目录")

    p_img = sub.add_parser("image", help="文生图（异步）")
    p_img.add_argument("prompt", help="提示词")
    p_img.add_argument("-o", "--out", default=str(OUT_DIR))
    p_img.add_argument("--model", default=env("FIREFLY_MODEL_IMAGE", DEFAULT_MODEL_IMAGE))
    p_img.add_argument("--model-version", default="2")
    p_img.add_argument("--n", type=int, default=1)
    p_img.add_argument("--size", default="auto", help="auto / 1024x1024 / 1792x1024 等")
    p_img.add_argument("--seeds", default="", help="逗号分隔的固定种子")
    p_img.add_argument("--detail-level", type=int, default=3)
    p_img.add_argument("--poll-interval", type=float, default=4.0)
    p_img.add_argument("--max-wait", type=float, default=900.0)

    p_vid = sub.add_parser("video", help="文生视频（异步）")
    p_vid.add_argument("prompt", help="提示词")
    p_vid.add_argument("-o", "--out", default=str(OUT_DIR))
    p_vid.add_argument("--model", default=env("FIREFLY_MODEL_VIDEO", DEFAULT_MODEL_VIDEO))
    p_vid.add_argument("--model-version", default="1")
    p_vid.add_argument("--n", type=int, default=1)
    p_vid.add_argument("--duration", type=int, default=8, help="视频时长秒")
    p_vid.add_argument("--aspect-ratio", default="16:9")
    p_vid.add_argument("--size", default="", help="如 1280x720；空则按比例推断")
    p_vid.add_argument("--no-audio", action="store_true")
    p_vid.add_argument("--seeds", default="")
    p_vid.add_argument("--poll-interval", type=float, default=6.0)
    p_vid.add_argument("--max-wait", type=float, default=1800.0)

    p_batch = sub.add_parser("batch", help="批量（每行一个 prompt）")
    p_batch.add_argument("prompts", help="prompts 文件路径")
    p_batch.add_argument("--kind", choices=["image", "video"], default="image")
    p_batch.add_argument("-o", "--out", default=str(OUT_DIR))
    p_batch.add_argument("--model", default="")
    p_batch.add_argument("--model-version", default="")
    p_batch.add_argument("--n", type=int, default=1)
    p_batch.add_argument("--size", default="auto")

    args = parser.parse_args()
    out_dir = Path(args.out)

    try:
        if args.cmd == "list":
            run_list(out_dir)
        elif args.cmd == "image":
            seeds = (
                [int(s) for s in args.seeds.split(",") if s.strip()]
                if args.seeds else None
            )
            run_image(
                args.prompt, out_dir,
                model=args.model,
                model_version=args.model_version,
                n=args.n,
                size=args.size,
                seeds=seeds,
                detail_level=args.detail_level,
                poll_interval=args.poll_interval,
                max_wait=args.max_wait,
            )
        elif args.cmd == "video":
            seeds = (
                [int(s) for s in args.seeds.split(",") if s.strip()]
                if args.seeds else None
            )
            run_video(
                args.prompt, out_dir,
                model=args.model,
                model_version=args.model_version,
                n=args.n,
                seeds=seeds,
                duration=args.duration,
                size=args.size,
                aspect_ratio=args.aspect_ratio,
                generate_audio=not args.no_audio,
                poll_interval=args.poll_interval,
                max_wait=args.max_wait,
            )
        elif args.cmd == "batch":
            defaults = (
                (env("FIREFLY_MODEL_IMAGE", DEFAULT_MODEL_IMAGE), "2")
                if args.kind == "image"
                else (env("FIREFLY_MODEL_VIDEO", DEFAULT_MODEL_VIDEO), "1")
            )
            model = args.model or defaults[0]
            model_version = args.model_version or defaults[1]
            run_batch(
                Path(args.prompts), out_dir,
                kind=args.kind, model=model, model_version=model_version,
                n=args.n, size=args.size,
            )
    except Exception as e:
        print(f"[错误] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()