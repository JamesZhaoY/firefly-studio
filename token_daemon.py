"""
adobe_token_daemon.py - 后台守护, 定期刷新 Firefly Bearer Token

原理:
  headed 浏览器登录一次 (手动解 CAPTCHA), 保存 storage_state。
  headless 守护每 N 秒 page.reload() 触发 Firefly 重取 token,
  Playwright 拦截 /signin/v2/tokens 响应, 原子写入 current_token.json。
  firefly_pipeline.py 每次调用都从该文件读最新 token。

用法:
  # 一次性: headed 登录 + 进入 headless 守护 (推荐)
  python adobe_token_daemon.py --start

  # 另开 shell 拿当前 token
  python adobe_token_daemon.py --get
  # 末行: FIREFLY_TOKEN=...

  # 守护 + HTTP 服务 (供其它进程直接 GET)
  python adobe_token_daemon.py --start --serve 5051
  # curl http://127.0.0.1:5051/token

  # 不登录, 只跑守护 (假设 storage 已存在)
  python adobe_token_daemon.py --run

参数:
  --interval SEC    刷新间隔秒 (默认 3000 = 50 分钟, 比 token 寿命 3600s 提前)
  --serve PORT      启动 HTTP 服务 (GET /token, GET /health)
  --storage PATH    storage_state 路径
  --token-file PATH token 输出路径
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import Response, sync_playwright

APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"

FIREFLY_URL = "https://firefly.adobe.com"
IMS_TOKEN_PATH = "/signin/v2/tokens"
STORAGE = Path(os.environ.get("ADOBE_STORAGE", str(DATA_DIR / "storage.json")))
TOKEN_FILE = Path(os.environ.get("ADOBE_TOKEN_FILE", str(DATA_DIR / "current_token.json")))
DEFAULT_INTERVAL = 3000  # 50 分钟


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


# def creds() -> tuple[str, str]:
#     u, p = env("ADOBE_USER"), env("ADOBE_PASS")
#     if not u or not p:
#         print("[错误] 设置环境变量 ADOBE_USER 和 ADOBE_PASS", file=sys.stderr)
#         sys.exit(1)
#     return u, p


def make_arp_capture(target: dict, *, required_url_contains: str = "/firefly/"):
    """拦截 page 请求, 抓 x-arp-session-id 头 (Adobe anti-replay token)。
    只接受 firefly API 的请求 (避免拿到无关 token)。
    """
    seen_log: list[tuple[str, dict]] = []

    def on_request(req) -> None:
        try:
            # 只关心 Firefly 自己的 API 请求
            if required_url_contains not in (req.url or ""):
                return
            headers = req.headers
            # 记录前 5 个 firefly 请求的 header (调试用)
            if len(seen_log) < 5:
                seen_log.append((req.url[:80], dict(headers)))
                target["_arp_debug"] = seen_log
            for k, v in headers.items():
                kl = k.lower()
                if kl in ("x-arp-session-id", "x-arp-id", "arp-session-id",
                         "x-adobe-arp-session-id", "x-ims-arp-session-id"):
                    if v and v.startswith("eyJ"):
                        # 过滤掉明显无效的占位值 (UUID 之类)
                        target["arp_session_id"] = v
                        target["_arp_header_name"] = k
                        return
        except Exception:
            pass

    return on_request


def _extract_arp_from_js(page) -> str | None:
    """兜底: Adobe IMS 客户端把 session 暴露在 window.adobeIMS 上,
    或者藏在 sessionStorage / localStorage 里。返回 None 时把调试信息落盘。"""
    """Adobe IMS JS 不暲露 x-arp-session-id, 此函数只用来诊断 IMS 内部状态。
    真正的 ARP 只能通过 page.on('request') 抓取 header。
    返回 None 是预期行为。
    """
    return None


def make_capture(target: dict):    return None


def make_capture(target: dict):
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
                target["expires_in"] = body.get("expiresIn")
                target["captured_at"] = time.time()
        except Exception:
            pass

    return on_response


def write_token_atomic(data: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    payload = {
        "token": data["token"],
        "expires_in": data.get("expires_in", 3600),
        "expires_at": data["captured_at"] + data.get("expires_in", 3600),
        "captured_at": data["captured_at"],
        "refresh_count": data.get("refresh_count", 0),
    }
    # 一起存 arp_session_id (Adobe anti-replay token), pipeline 需要
    arp = data.get("arp_session_id")
    if arp:
        payload["arp_session_id"] = arp
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(TOKEN_FILE)


def read_token() -> dict | None:
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def launch_browser(headless: bool):
    return p.chromium.launch(  # noqa: F821 — sync_playwright() ctx
        headless=not headless,
        channel="chrome",
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    )


def _dump_page_state(page) -> None:
    """超时诊断: 截图 + 打印 URL/标题/可见 input。"""
    out_dir = STORAGE.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    shot = out_dir / "debug_login.png"
    try:
        page.screenshot(path=str(shot), full_page=True)
        print(f"        截图: {shot}")
    except Exception as e:
        print(f"        (截图失败: {e})")

    print(f"        URL:   {page.url}")
    print(f"        标题:  {page.title()!r}")
    try:
        inputs = page.evaluate(
            """() => [...document.querySelectorAll('input,button')]
                .filter(el => el.offsetParent !== null)
                .slice(0, 20)
                .map(el => ({
                    tag: el.tagName,
                    type: el.type || '',
                    name: el.name || '',
                    id: el.id || '',
                    placeholder: el.placeholder || '',
                    disabled: !!el.disabled,
                    text: (el.innerText || '').slice(0, 30),
                }))"""
        )
        print(f"        可见元素 ({len(inputs)}):")
        for inp in inputs:
            print(f"          {inp}")
    except Exception as e:
        print(f"        (元素列表失败: {e})")


def _probe_session_storage(page) -> str | None:
    """兜底: 登录成功后 Firefly 把 token 放在 sessionStorage / 内存里。"""
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
            # 试着抠 JWT
            import re as _re

            m = _re.search(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", v)
            if m:
                return m.group(0)
    return None


def do_login(headed: bool = True) -> None:
    target: dict = {}
    with sync_playwright() as p:
        print(f"[1/4] 启动 headed 浏览器 () ...")
        browser = p.chromium.launch(
            headless=not headed,
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
        page.on("response", make_capture(target))
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
            print()
        else:
            print("[3/4] 已登录状态, 等待 token 刷新 ...")

        print("[4/4] 等待登录完成 (最长 10 分钟, 每 5 秒报告状态) ...")
        deadline = time.time() + 600
        last_url = ""
        last_status = 0
        sess_token: str | None = None
        while time.time() < deadline:
            time.sleep(5)
            elapsed = int(deadline - time.time())
            cur_url = page.url

            if cur_url != last_url:
                print(f"        [{600-elapsed:3d}s剩] URL 变化 → {cur_url}")
                last_url = cur_url

            # 主路径: 拦截 /signin/v2/tokens 响应
            if target.get("token"):
                print(f"        [{600-elapsed:3d}s剩] ✓ token 已从响应捕获")
                break

            # 兜底: 已跳回 firefly.adobe.com 时, 抓 sessionStorage
            if "firefly.adobe.com" in cur_url and "/signin" not in cur_url:
                if sess_token is None:
                    sess_token = _probe_session_storage(page)
                if sess_token:
                    print(f"        [{600-elapsed:3d}s剩] ✓ token 从 sessionStorage 兜底捕获")
                    target["token"] = sess_token
                    target["expires_in"] = 3600
                    target["captured_at"] = time.time()
                # 兜底抓 arp_session_id (request header 没抓到时, 从 JS 找)
                if not target.get("arp_session_id"):
                    arp = _extract_arp_from_js(page)
                    if arp:
                        target["arp_session_id"] = arp
                        print(f"        [{600-elapsed:3d}s剩] ✓ arp_session_id 从 JS 兜底捕获 (长度 {len(arp)})")
                if sess_token:
                    break
                # 周期性提示
                if int(time.time()) % 15 < 5:
                    print(f"        [{600-elapsed:3d}s剩] 在 firefly.adobe.com 上, 找 token ...")

            # 周期性提示
            now = int(time.time())
            if now - last_status >= 30:
                last_status = now
                print(
                    f"        [{600-elapsed:3d}s剩] 等待中 ... 当前页: "
                    f"{cur_url[cur_url.find('/', 9):][:50] if '/' in cur_url[9:] else cur_url}"
                )

        if not target.get("token"):
            print()
            print("[诊断] 10 分钟内未捕获 token, 排查以下信息:")
            _dump_page_state(page)
            browser.close()
            sys.exit(1)

        time.sleep(2)
        STORAGE.parent.mkdir(parents=True, exist_ok=True)
        ctx.storage_state(path=str(STORAGE))
        target["refresh_count"] = 0
        write_token_atomic(target)
        print(f"[OK] storage 已保存: {STORAGE}")
        print(f"[OK] token 已写入: {TOKEN_FILE}")
        browser.close()


def run_daemon(refresh_interval: int, http_port: int | None) -> None:
    if not STORAGE.exists():
        sys.exit(f"[错误] 未找到 {STORAGE}, 先跑 --login 或 --start")

    target: dict = {}
    refresh_count = 0
    stop_event = threading.Event()

    with sync_playwright() as p:
        print(f"[1/3] 启动 headless 浏览器, 加载 storage: {STORAGE}")
        browser = p.chromium.launch(
            headless=True,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            storage_state=str(STORAGE),
        )
        page = ctx.new_page()
        page.on("response", make_capture(target))
        page.on("request", make_arp_capture(target))

        print(f"[2/3] 访问 {FIREFLY_URL} ...")
        try:
            page.goto(FIREFLY_URL, wait_until="networkidle", timeout=120_000)
        except Exception as e:
            print(f"[警告] networkidle 超时: {e}, 改 domcontentloaded")
            page.goto(FIREFLY_URL, wait_until="domcontentloaded", timeout=120_000)

        if "auth.services.adobe.com" in page.url:
            browser.close()
            sys.exit(
                "[错误] storage 已失效 (跳回登录页)。请重跑 --start 重新登录"
            )

        # 等首次刷新 (页面刚加载, 可能立即触发 token 刷新,
        # 也可能不触发 — 因为 headed 拿到的 token 才几秒前, 离过期还早)
        deadline = time.time() + 30
        while time.time() < deadline and not target.get("token"):
            time.sleep(0.5)

        if target.get("token"):
            refresh_count += 1
            target["refresh_count"] = refresh_count
            write_token_atomic(target)
            print(f"[OK] 首次刷新捕获 (refresh #{refresh_count})")
        else:
            # 没触发刷新 — 复用 headed 刚拿到的 token
            existing = read_token()
            if existing and existing.get("expires_at", 0) > time.time() + 60:
                remain = int((existing["expires_at"] - time.time()) / 60)
                print(f"[信息] 未触发刷新, 复用 headed 登录的 token (剩余 {remain} 分钟)")
                target.update(existing)
            else:
                browser.close()
                sys.exit(
                    "[错误] 未捕获 token 且 current_token.json 无有效 token, 重跑 --start"
                )

        if http_port:
            t = threading.Thread(
                target=_start_http_server,
                args=(http_port, stop_event),
                daemon=True,
            )
            t.start()
            print(f"[OK] HTTP 服务: http://127.0.0.1:{http_port}/token")

        print(
            f"[3/3] 进入刷新循环, 间隔 {refresh_interval}s "
            f"({refresh_interval // 60} 分钟), Ctrl+C 退出"
        )
        try:
            while not stop_event.is_set():
                # 分片 sleep, 以便响应 stop_event / Ctrl+C
                slept = 0
                while slept < refresh_interval and not stop_event.is_set():
                    time.sleep(min(5, refresh_interval - slept))
                    slept += 5

                if stop_event.is_set():
                    break

                refresh_count += 1
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"\n[刷新 #{refresh_count}] {ts} reload ...")
                target.clear()

                try:
                    page.reload(wait_until="networkidle", timeout=60_000)
                except Exception as e:
                    print(f"[警告] reload 失败: {e}")
                    continue

                if "auth.services.adobe.com" in page.url:
                    print("[警告] 跳回登录页, storage 失效")
                    print("       请 Ctrl+C 退出, 然后重跑 --start")
                    break

                deadline = time.time() + 30
                while time.time() < deadline and not target.get("token"):
                    time.sleep(0.5)

                if not target.get("token"):
                    print("[警告] 未捕获到新 token, 下次重试")
                    continue

                target["refresh_count"] = refresh_count
                write_token_atomic(target)
                remain = int(target["captured_at"] + target.get("expires_in", 3600) - time.time())
                print(f"[OK] token 已更新, 剩余 {remain}s")
        except KeyboardInterrupt:
            print("\n[信息] Ctrl+C, 退出")
        finally:
            stop_event.set()
            try:
                browser.close()
            except Exception:
                pass


def _start_http_server(port: int, stop_event: threading.Event) -> None:
    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/token":
                tok = read_token()
                if not tok:
                    self.send_error(503, "token not ready")
                    return
                body = json.dumps(tok, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/health":
                tok = read_token()
                ok = bool(tok)
                exp = (tok or {}).get("expires_at", 0)
                body = json.dumps(
                    {
                        "status": "ok" if ok else "no-token",
                        "expires_at": exp,
                        "now": time.time(),
                        "expired": bool(exp and time.time() > exp),
                    }
                ).encode("utf-8")
                self.send_response(200 if ok else 503)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def log_message(self, *args, **kwargs) -> None:  # noqa: D401
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), H)
    try:
        server.serve_forever()
    finally:
        server.shutdown()


def cmd_get() -> None:
    tok = read_token()
    if not tok:
        sys.exit("[错误] token 文件不存在, 守护未运行? 试 --start")
    if tok.get("expires_at") and time.time() > tok["expires_at"]:
        exp = datetime.fromtimestamp(tok["expires_at"]).isoformat()
        print(f"[警告] token 已过期 ({exp})", file=sys.stderr)
    print(f"FIREFLY_TOKEN={tok['token']}")


def cmd_start(args) -> None:
    do_login(headed=True)
    print()
    run_daemon(refresh_interval=args.interval, http_port=args.serve)


def cmd_run(args) -> None:
    run_daemon(refresh_interval=args.interval, http_port=args.serve)


def main() -> None:
    global STORAGE, TOKEN_FILE

    ap = argparse.ArgumentParser(
        description="Adobe IMS 后台守护, 定期刷新 Firefly Token"
    )
    ap.add_argument("--start", action="store_true", help="登录一次 + 启动守护 (推荐)")
    ap.add_argument("--run", action="store_true", help="只启动守护 (假设 storage 已存在)")
    ap.add_argument("--get", action="store_true", help="读取当前 token")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help=f"刷新间隔秒 (默认 {DEFAULT_INTERVAL})")
    ap.add_argument("--serve", type=int, metavar="PORT", help="启动 HTTP 服务")
    ap.add_argument("--storage", default=str(STORAGE))
    ap.add_argument("--token-file", default=str(TOKEN_FILE))
    args = ap.parse_args()

    STORAGE = Path(args.storage)
    TOKEN_FILE = Path(args.token_file)

    cmds = sum(bool(x) for x in (args.start, args.run, args.get))
    if cmds != 1:
        ap.print_help()
        sys.exit(1)

    try:
        if args.get:
            cmd_get()
        elif args.start:
            cmd_start(args)
        else:
            cmd_run(args)
    except Exception as e:
        print(f"[错误] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()