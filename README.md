# Adobe Firefly Studio

Self-hosted web console for Adobe Firefly 3P image / video generation.

- **Backend** Flask + SQLite (jobs + 调用日志)
- **Frontend** React + Vite, teal-on-zinc dark UI with persistent left sidebar
- **产物只记下载 URL**，服务端不落盘任何文件

```text
adobe/
  app.py                  # Flask API :7860
  db.py                   # SQLite
  firefly_pipeline.py     # 上游 Firefly 客户端
  models_catalog.py       # 模型展开 + 预设
  token_daemon.py         # Playwright 登录 + cookie 刷新
  data/                   # 运行期数据（git 忽略）
    storage.json
    current_token.json
    firefly.db
  frontend/               # React + Vite
    src/
    package.json
    vite.config.js
```

---

## 安装

```bash
# 1. Python 依赖（含 curl_cffi 模拟浏览器 TLS 指纹）
pip install -r requirements.txt
playwright install chromium

# 2. 前端依赖
cd frontend
npm install
```

---

## 启动

### 1) 登录拿 cookie（首次 / cookie 失效时）

```bash
python token_daemon.py --start
```

Chrome 弹出 → 在 `firefly.adobe.com` 解 CAPTCHA + 登录 → 自动写 `data/storage.json` 和 `data/current_token.json`。可 `Ctrl+C` 退出，后台刷新可单独跑 `python token_daemon.py --run`。

### 2) 启动后端

```bash
python app.py
# http://127.0.0.1:7860
```

### 3) 启动前端

```bash
cd frontend
npm run dev
# http://localhost:5173（已代理 /api → 7860）
```

浏览器打开 **http://localhost:5173**。

---

## Web UI 功能

- **对话**：左侧持久 sidebar（对话 / 探索 / 日志），右侧 Chat 风格消息流 + 底部 composer
- **探索**：模型库网格，可点选跳回对话
- **日志**：调用日志列表，支持清空
- **清空对话** / **清空日志**：每个页面 topbar 右侧的垃圾箱按钮（带确认）
- **快捷键**：`⌘↵` 提交 / `⌘/` 展开参数 / `⌘.` 切图片/视频

---

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 / 登录态 |
| GET | `/api/models` | 模型列表（`?refresh=1` 强制上游） |
| POST | `/api/generate` | 提交生成（异步） |
| GET | `/api/jobs` | 任务列表 |
| GET | `/api/jobs/<id>` | 任务详情（含 `outputs[].url`） |
| DELETE | `/api/jobs/<id>` | 删除任务 |
| POST | `/api/chat/clear` | 清空所有任务及其日志 |
| GET | `/api/logs` | 调用日志（`?job_id=`） |
| DELETE | `/api/logs` | 清空所有调用日志 |

### 生成示例

```json
POST /api/generate
{
  "kind": "video",
  "prompt": "火星上的宇航员第一人称视角",
  "model": "seedance",
  "model_version": "seedance_2.0",
  "duration": 6,
  "aspect_ratio": "16:9",
  "size": "854x480",
  "generate_audio": false
}
```

成功后任务 `outputs` 形如：

```json
[
  { "type": "video", "url": "https://pre-signed-....mp4", "ext": ".mp4" }
]
```

前端直接用 URL 预览 / 新开标签下载。

### 调用日志阶段

每次生成会按顺序写 4 条 `api_logs` 行：

| phase | 内容 |
|-------|------|
| `request_params` | 归一化后的请求参数 |
| `task_created` | 上游返回的 task_id / poll_url / HTTP code |
| `task_succeeded` | 成功：files 数组（URL + 类型） |
| `task_failed` + `task_traceback` | 失败：异常类型 / 上游错误码 / Python 堆栈 |

---

## SQLite

`data/firefly.db`

- `jobs` — 任务状态、参数、outputs（URL JSON）
- `api_logs` — 每次生成的 4 个阶段

---

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `FLASK_HOST` | `127.0.0.1` | API 监听 |
| `FLASK_PORT` | `7860` | API 端口 |
| `FLASK_DEBUG` | `0` | Flask debug 开关 |
| `VITE_API_BASE` | 空（走代理） | 前端直连 API 时设 `http://127.0.0.1:7860` |
| `FIREFLY_STORAGE` | `data/storage.json` | Playwright cookie |
| `FIREFLY_TOKEN_FILE` | `data/current_token.json` | IMS token 缓存 |

---

## 生产部署

前后端拆开：前端 → GitHub Pages（已配 `.github/workflows/pages.yml`），后端 → Render / Fly.io 二选一。

### 选项 A：后端 → Render（$7/月 Starter 计划）

> Free tier **不能用**：没有持久磁盘，SQLite + cookie 重启就丢。

`render.yaml` 已写好。Render 控制台 → New → Blueprint → 指 `render.yaml`。或手动：

| 字段 | 值 |
|------|---|
| Plan | Starter ($7/mo) |
| Build Command | `pip install -r requirements.txt` |
| Start Command | 见 `render.yaml`（自动拷 secret 文件到 data dir 后启 gunicorn） |
| Health Check Path | `/api/health` |

环境变量已在 `render.yaml` 里。Secret Files 上传：

- `storage.json`（内容 = 本地 `data/storage.json` 全文）
- `current_token.json`（内容 = 本地 `data/current_token.json` 全文）

Disks → Add：`firefly-data` / `/var/data` / 1GB。

### 选项 B：后端 → Fly.io（**真免费**，1GB 持久卷）

`fly.toml` + `Dockerfile` + `fly-setup.md` 已写好。

```bash
# 安装 flyctl: https://fly.io/docs/flyctl/install/
fly auth signup          # 不需要信用卡
cd D:\workspace\app\adobe
fly launch --no-deploy   # 创建 app，复制 fly.toml
fly volumes create firefly_data --size 1 --region nrt
fly secrets set STORAGE_JSON="$(cat data/storage.json)"
fly secrets set TOKEN_JSON="$(cat data/current_token.json)"
fly deploy
```

> Free 计划允许 1 个 shared-cpu-1x VM（256MB），1GB 卷。sleep on idle 不收费。

### 前端 → GitHub Pages

已自动部署：https://jameszhaoy.github.io/firefly-studio/

要让前端连后端：

- Repo → Settings → Secrets and variables → Actions → New secret:
  - `VITE_API_BASE` = `https://firefly-api-xxxx.onrender.com`（或 Fly 给的 `https://firefly-studio.fly.dev`）

下次 push 自动重新 build 并生效。

---

## 常见问题

- **NameError: name 'os' is not defined**：`app.py` 漏 `import os`。已在 master 修。
- **Render 没有 disk 目录**：free tier 没有磁盘。改用 Starter $7/月 或 Fly.io 免费。

- **408 system under load**：token client_id 与 `x-api-key` 不匹配 / 缺 `x-arp-session-id` / 没装 `curl_cffi` 等。本仓库默认选 token 文件里的 client_id 并自动生成 base64 arp。
- **额度耗尽**：上游返回 `401/403` + header `x-access-error=taste_exhausted`，前端会显示对应文案。
- **playwright 没装浏览器**：`playwright install chromium`。