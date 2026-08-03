"""
adobe_token_daemon.py - 一次性登录脚本, 抓 token 和 cookie 后退出.

原理:
  headed 浏览器登录一次 (手动解 CAPTCHA), 拦截 /signin/v2/tokens 拿到 Bearer,
  Playwright storage_state 拿 cookie, 两者原子写入同名前缀的 JSON 文件.
  用户把这两个文件拿去账号池页面 (或调 /api/accounts/upload) 即可.

用法:
  # 默认: 写到 ./data/<name>_token.json + ./data/<name>_cookie.json
  python token_daemon.py --login
  python token_daemon.py --login --name alice

  # 写到自定义目录
  python token_daemon.py --login --name alice --out-dir ~/certs

  # 另开 shell 拿当前 token (从最近写入的 *_token.json 中选最新一个)
  python token_daemon.py --get
  python token_daemon.py --get --name alice

参数:
  --login           启动 headed 浏览器, 登录, 抓 token + cookie, 退出 (默认)
  --get             读取已有 token 文件, 输出 FIREFLY_TOKEN=...
  --name NAME       文件名前缀, 默认 'firefly'
  --out-dir DIR     输出目录, 默认 ./data
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import Response, sync_playwright

APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"

FIREFLY_URL = "https://firefly.adobe.com"
IMS_TOKEN_PATH = "/signin/v2/tokens"

DEFAULT_NAME = "firefly"
DEFAULT_OUT_DIR = DATA_DIR


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def make_arp_capture(target: dict, *, required_url_contains: str = "/firefly/"):
    """拦截 page 请求, 抓 x-arp-session-id 头 (Adobe anti-replay token)."""

    def on_request(req) -> None:
        try:
            if required_url_contains not in (req.url or ""):
                return
            for k, v in req.headers.items():
                kl = k.lower()
                if kl in (
                    "x-arp-session-id",
                    "x-arp-id",
                    "arp-session-id",
                    "x-adobe-arp-session-id",
                    "x-ims-arp-session-id",
                ):
                    if v and v.startswith("eyJ"):
                        target["arp_session_id"] = v
                        target["_arp_header_name"] = k
                        return
        except Exception:
            pass

    return on_request


def make_token_capture(target: dict):
    """拦截 /signin/v2/tokens 响应, 抓 Bearer."""

    def on_response(resp: Response) -> None:
        try:
            if IMS_TOKEN_PATH not in resp.url:
                return
            if resp.request.method != "POST":
                return
            if resp.status >= 400:
                return
            body = resp.json()
            tok = body.get("token")
            if tok:
                target["token"] = tok
                target["expires_in"] = body.get("expiresIn") or 3600
                target["captured_at"] = time.time()
        except Exception:
            pass

    return on_response


def _probe_session_storage(page) -> str | None:
    """兜底: Firefly 偶尔把 token 放 sessionStorage 里。"""
    try:
        items = page.evaluate(
            """() => {
                const out = {};
                try { for (let i=0;i<sessionStorage.length;i++){const k=sessionStorage.key(i);out['s_'+k]=sessionStorage.getItem(k);} } catch(e){}
                try { for (let i=0;i<localStorage.length;i++){const k=localStorage.key(i);out['l_'+k]=localStorage.getItem(k);} } catch(e){}
                return out;
            }"""
        )
    except Exception:
        return None
    for k, v in (items or {}).items():
        if not isinstance(v, str):
            continue
        if "eyJ" in v and ("adobe" in v.lower() or "firefly" in v.lower() or k.lower().endswith("token")):
            import re as _re

            m = _re.search(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", v)
            if m:
                return m.group(0)
    return None


def _dump_page_state(page) -> None:
    """诊断: 截图 + 打印 URL/标题/可见元素。"""
    out_dir = DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    shot = out_dir / "debug_login.png"
    try:
        page.screenshot(path=str(shot), full_page=True)
        print(f"        截图: {shot}")
    except Exception as e:
        print(f"        (截图失败: {e})")
    print(f"        URL:   {page.url}")
    print(f"        标题:  {page.title()!r}")


def login_and_dump(name: str, out_dir: Path) -> tuple[Path, Path]:
    """headed 浏览器登录, 写 token + cookie, 返回两个文件路径."""
    token_path = out_dir / f"{name}_token.json"
    cookie_path = out_dir / f"{name}_cookie.json"

    target: dict = {}
    with sync_playwright() as p:
        print(f"[1/4] 启动 headed 浏览器 ...")
        browser = p.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        page.on("response", make_token_capture(target))
        page.on("request", make_arp_capture(target))

        print(f"[2/4] 打开 {FIREFLY_URL} ...")
        page.goto(FIREFLY_URL, wait_until="domcontentloaded", timeout=120_000)
        time.sleep(3)

        if "auth.services.adobe.com" in page.url:
            print("[3/4] 检测到登录页 — 操作步骤:")
            print("        ① 等 Arkose CAPTCHA 加载完 (5-10 秒)")
            print("        ② 解 CAPTCHA (按提示拖动/点图)")
            print("        ③ 邮箱框变成可输入 → 填邮箱 → 点 Continue")
            print("        ④ 可能还有第二个 CAPTCHA → 解开")
            print("        ⑤ 输密码 → 点 Sign in")
            print()
            print("        注意: CAPTCHA 没解完时登录按钮是禁用的, 点了无反应")
        else:
            print("[3/4] 已登录状态, 等待 token 刷新 ...")

        print("[4/4] 等待登录完成 (最长 10 分钟, 每 5 秒报告状态) ...")
        deadline = time.time() + 600
        last_url = ""
        sess_token: str | None = None
        while time.time() < deadline:
            time.sleep(5)
            elapsed = int(deadline - time.time())
            cur_url = page.url
            if cur_url != last_url:
                print(f"        [{600-elapsed:3d}s剩] URL 变化 → {cur_url}")
                last_url = cur_url
            if target.get("token"):
                print(f"        [{600-elapsed:3d}s剩] ✓ token 已从响应捕获")
                break
            if "firefly.adobe.com" in cur_url and "/signin" not in cur_url:
                if sess_token is None:
                    sess_token = _probe_session_storage(page)
                if sess_token:
                    print(f"        [{600-elapsed:3d}s剩] ✓ token 从 sessionStorage 兜底捕获")
                    target["token"] = sess_token
                    target["expires_in"] = 3600
                    target["captured_at"] = time.time()
                    break

        if not target.get("token"):
            print()
            print("[诊断] 10 分钟内未捕获 token, 排查以下信息:")
            _dump_page_state(page)
            browser.close()
            sys.exit(1)

        time.sleep(2)
        out_dir.mkdir(parents=True, exist_ok=True)
        ctx.storage_state(path=str(cookie_path))

        payload = {
            "token": target["token"],
            "expires_in": target.get("expires_in", 3600),
            "expires_at": target["captured_at"] + target.get("expires_in", 3600),
            "captured_at": target["captured_at"],
        }
        arp = target.get("arp_session_id")
        if arp:
            payload["arp_session_id"] = arp
        _write_atomic(token_path, payload)

        print(f"[OK] cookie 已保存: {cookie_path}")
        print(f"[OK] token 已保存:  {token_path}")
        print()
        print("下一步: 把这两个文件上传到「账号池」页面, 或:")
        print(f"  curl -X POST .../api/accounts/upload \\")
        print(f"    -F label={name} -F token_file=@{token_path} -F cookie_file=@{cookie_path}")

        browser.close()
    return token_path, cookie_path


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def find_token_file(name: str, out_dir: Path) -> Path | None:
    """定位 token 文件: 优先 <name>_token.json, 否则该目录下最新 *_token.json。"""
    exact = out_dir / f"{name}_token.json"
    if exact.exists():
        return exact
    candidates = sorted(out_dir.glob("*_token.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def cmd_get(name: str, out_dir: Path) -> None:
    path = find_token_file(name, out_dir)
    if not path:
        sys.exit(f"[错误] 找不到 token 文件 ({name}_token.json), 先跑 --login")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        sys.exit(f"[错误] 解析 {path} 失败: {e}")
    if data.get("expires_at") and time.time() > data["expires_at"]:
        print(f"[警告] {path.name} 已过期", file=sys.stderr)
    print(f"FIREFLY_TOKEN={data['token']}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="一次性 Adobe IMS 登录脚本: 生成 token + cookie JSON 文件"
    )
    ap.add_argument("--login", action="store_true", help="启动 headed 登录并写入文件 (默认动作)")
    ap.add_argument("--get", action="store_true", help="读取最新 token 文件, 输出 FIREFLY_TOKEN=...")
    ap.add_argument("--name", default=DEFAULT_NAME, help=f"文件名前缀, 默认 '{DEFAULT_NAME}'")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="输出目录, 默认 ./data")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    name = args.name.strip() or DEFAULT_NAME

    if args.get:
        cmd_get(name, out_dir)
        return
    # 默认动作: --login
    login_and_dump(name, out_dir)


if __name__ == "__main__":
    main()