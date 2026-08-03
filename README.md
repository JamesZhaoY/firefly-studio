# Adobe Firefly Studio

Self-hosted web console for Adobe Firefly 3P image / video generation.

- **Backend** Flask + SQLite (jobs + 调用日志)
- **Frontend** React + Vite, teal-on-zinc dark UI with persistent left sidebar
- **多账号连接池** round-robin + 失败自动冷却/切换（详见下文）
- **普通生成**只记录下载 URL；**一键成片**会在 `outputs/` 保存合成视频和中间产物

```text
adobe/
  app.py                  # Flask API :7860
  db.py                   # SQLite
  firefly_pipeline.py     # 上游 Firefly 客户端
  token_pool.py           # 多账号连接池 (新增)
  models_catalog.py       # 模型展开 + 预设
  video_pipeline.py       # 一键成片（拆分 → 镜头视频 → TTS → 拼接）
  token_daemon.py         # Playwright 登录 + cookie 刷新
  tests/                  # 纯函数测试
  data/                   # 运行期数据（git 忽略）
    storage.json          # token_daemon 生成的 cookie（上传前的临时文件）
    current_token.json    # token_daemon 生成的 token（上传前的临时文件）
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

### 1) 上传账号（首次 / 新增账号）

打开前端 **账号池** 页面，上传 `current_token.json`（必填）+ `storage.json`（可选）即可。
不再从本地 `data/current_token.json` / `data/storage.json` 自动读取 —— 所有凭证由本页面统一管理。
token 文件可通过 `token_daemon.py --start` 拿到，或直接复用现有 IMS 刷新脚本的产物。

```bash
# 仅在需要拿新 token 时跑（仍可单独用）
python token_daemon.py --start
# Chrome 弹出 → 在 firefly.adobe.com 解 CAPTCHA + 登录 →
# 自动写 data/current_token.json + data/storage.json
# 把这两个文件拿到前端「账号池」页面上传即可
```

### 2) 启动后端

```bash
python app.py
# http://127.0.0.1:7860
```

启动时若 pool 为空会打印：

```
accounts_pool: size=0 available=0 (池为空, 所有请求都会 400; 请到「账号池」页面上传)
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
- **一键成片**：对话 topbar 左侧按钮，弹出 slide-over 表单 → 提交 → 自动轮询，3-15 分钟出成片，可关闭面板后台运行
- **快捷键**：`⌘↵` 提交 / `⌘/` 展开参数 / `⌘.` 切图片/视频

---

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 / 登录态 / pool 摘要 |
| GET | `/api/models` | 模型列表（`?refresh=1` 强制上游） |
| POST | `/api/generate` | 提交生成（异步） |
| GET | `/api/jobs` | 任务列表 |
| GET | `/api/jobs/<id>` | 任务详情（含 `outputs[].url`） |
| DELETE | `/api/jobs/<id>` | 删除任务 |
| POST | `/api/chat/clear` | 清空所有任务及其日志 |
| GET | `/api/logs` | 调用日志（`?job_id=`） |
| DELETE | `/api/logs` | 清空所有调用日志 |
| POST | `/api/video/generate` | 一键成片（文字 → 多镜头视频 + 配音） |
| GET | `/api/video/<job_id>` | 一键成片任务详情（含 `final_video_path`） |
| GET | `/api/voices` | TTS 可用语音列表（Microsoft Edge TTS via `edge-tts`） |
| GET | `/api/llm-models` | 从 LLM 服务的 `/v1/models` 获取分镜模型列表 |
| GET  | `/api/accounts` | 列出账号池 + 健康状态 |
| POST | `/api/accounts/upload` | 上传一个账号（multipart: `token_file` + 可选 `cookie_file` + `label`） |
| DELETE | `/api/accounts/<id>` | 删除账号 |
| PATCH | `/api/accounts/<id>` | 重命名 / 启停 `{label?, disabled?}` |
| POST | `/api/accounts/<id>/refresh` | 用本账号 cookie 强制 IMS 刷 token |
| GET | `/outputs/<path>` | 读取本地产物（成片 / 关键帧 / TTS） |

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

### 一键成片（文字 → 多镜头视频）

> 新增 `video_pipeline.py`：把一段自然语言描述自动拆成 3-6 个分镜，
> 每个分镜出镜头视频、再出 TTS 配音，最后用 `ffmpeg` 拼接 + 混音，
> 产物落在 `outputs/videos/<job_id>/final.mp4`，前端可直接 `<video>` 预览。
>
> TTS 由 `edge-tts` 提供（Microsoft Edge 公开接口），不需要 Adobe 账号。
> 任意镜头 TTS 失败时，自动用对应时长的静音兜底，不阻断合成；任务日志会记录具体失败项。

请求：

```bash
curl -X POST http://127.0.0.1:7860/api/video/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "prompt": "清晨森林里的小鹿走入薄雾，远处有鹿群奔过，最后太阳升起照亮山谷",
    "options": {
      "shot_count": 4,
      "duration_sec": 6,
      "voice": "zh-CN-XiaoxiaoNeural",
      "aspect_ratio": "16:9"
    }
  }'
```

返回（202）：

```json
{
  "job_id": "a1b2c3d4e5f6",
  "job": { "id": "a1b2c3d4e5f6", "status": "queued", "kind": "video_pipeline", ... }
}
```

轮询：

```bash
curl http://127.0.0.1:7860/api/video/<job_id>
```

`status="succeeded"` 时 `result.final_video_path` 是绝对路径，
前端可读 `outputs[]` 里的 `/outputs/videos/<job_id>/final.mp4` 直接预览。

`options` 字段（全部可选）：

| 字段 | 默认 | 说明 |
|------|------|------|
| `shot_count` | 启发式 3-6 | 镜头数，clamp 到 [3, 6] |
| `duration_sec` | `6` | 每个镜头目标时长（秒） |
| `voice` | `zh-CN-XiaoxiaoNeural` | TTS 语音（`GET /api/voices` 取全量） |
| `aspect_ratio` | `16:9` | 视频比例，影响镜头尺寸 |
| `video_model` / `video_model_version` | 空（用 fp 预设） | 镜头模型 |
| `generate_audio` | `true` | 镜头自带音轨（与 TTS 配音独立） |
| `use_llm` | `false` | `true` → 走 LLM 拆镜（见下文）；失败时自动回退到启发式 |

**降级策略**：若系统未装 `ffmpeg`，不会抛异常，而是写一份 `manifest.json`
到 `outputs/videos/<job_id>/`，前端能继续展示分镜进度与 URL。

### LLM 分镜（可选）

`options.use_llm=true` 时，`split_storyboard()` 改为调用 OpenAI 兼容的
`/chat/completions` 拿到结构化 JSON 数组，再落回原来的图/镜/TTS 流水线。
任意环节失败（LLM 未配置、超时、JSON 损坏）都会自动回退到原启发式拆分，
不会让成片任务失败。

请求体由 LLM 返回的字段：

```json
[ { "visual": "opening cinematic shot: ...", "narration": "清晨薄雾",
    "duration": 6 }, ... ]
```

环境变量：

| 变量 | 默认 | 说明 |
|------|------|------|
| `LLM_BASE_URL` | `http://127.0.0.1:8317/v1` | 兼容 `/chat/completions` 的服务地址 |
| `LLM_API_KEY` | `local-dev-key` | 鉴权 Bearer |
| `LLM_MODEL` | `gpt-5.5` | 模型名 |
| `LLM_TIMEOUT` | `20` | 单次请求超时（秒） |

Docker Desktop 中本地 LLM 跑在宿主机时，曾由 Compose 将 `LLM_BASE_URL` 配置为
`http://host.llm:8317/v1` 以避开容器内 `127.0.0.1`。手动部署下，宿主机直接
跑 LLM 服务即可，无需别名。

### 调用日志阶段

每次生成会按顺序写 4 条 `api_logs` 行：

| phase | 内容 |
|-------|------|
| `request_params` | 归一化后的请求参数 |
| `task_created` | 上游返回的 task_id / poll_url / HTTP code + 命中账号 |
| `task_succeeded` | 成功：files 数组（URL + 类型） |
| `task_failed` + `task_traceback` | 失败：异常类型 / 上游错误码 / Python 堆栈 |

---

## 多账号连接池

> 多账号上传即自动负载均衡：每个请求从健康账号中轮询取一个；上游报错（401/quota/限流）
> 自动按错误类型设置冷却时间，下个请求自动选下一个健康账号。

### 上传账号（推荐路径）

打开前端 **账号池** 页 → 选 `current_token.json`（必填）+ `storage.json`（可选）+ 标签 → 上传。
标签重名会自动加 `(2)` 后缀。也可调 API：

```bash
curl -X POST http://127.0.0.1:7860/api/accounts/upload \
  -F "label=alice" \
  -F "token_file=@./current_token.json" \
  -F "cookie_file=@./storage.json"
```

### 一键迁移老的单账号

迁移路径已移除。所有账号必须通过 `POST /api/accounts/upload` 上传；
不再从 `data/current_token.json` / `data/storage.json` 自动读取。

### 负载均衡 + 失败冷却

| 错误关键字 | 冷却时长 | 原因 |
|------|------|------|
| `401 / 403 / unauthorized / expired` | 60s | 鉴权问题（cookie 即将刷新） |
| `quota / credit / taste_exhausted / rate / limit` | 300s | 余额/限流，强制等额度重置 |
| `500 / 502 / 503 / 504` | 30s | 上游暂时不可用 |
| `408 / timeout / network` | 10s | 瞬时网络 |
| 其它 | 10s | 兜底 |

冷却时长随连续失败次数指数叠加（封顶 15 分钟）。轮询取号时跳过：
- `disabled=true`
- 冷却未结束
- 已过期

### 强制 IMS 刷新

每个账号可单独调 `POST /api/accounts/<id>/refresh` 用它自己的 cookie 刷 token；
成功后清空该账号的冷却。批量调度 / 守护可基于此自建。

### 落盘格式

账号凭证全部保存到 `data/firefly.db` 的 `accounts` 表，不再写入或读取
`data/accounts/*.json`。token/cookie 在 SQLite 的 `payload_json` 列中持久化，
不通过账号列表 API 返回。

健康度（cooldown、连续失败次数、统计）只在内存里，不落盘——重启后回到初始健康。

### 向后兼容

无。`data/current_token.json` / `data/storage.json` 不再被服务自动读取，
所有账号必须通过上传接口进入 pool；老的 daemon 脚本只用来**生成**这两个文件供上传使用。

---

## SQLite

`data/firefly.db`

- `jobs` — 任务状态、参数、outputs（URL JSON）
- `api_logs` — 每次生成的 4 个阶段
- `accounts` — 管理端上传的 token / cookie 账号池凭证

---

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `FLASK_HOST` | `127.0.0.1` | API 监听 |
| `FLASK_PORT` | `7860` | API 端口 |
| `FLASK_DEBUG` | `0` | Flask debug 开关 |
| `VITE_API_BASE` | 空（走代理） | 前端直连 API 时设 `http://127.0.0.1:7860` |
| `ADMIN_API_KEY` | 空（本地开发不校验） | API 管理密钥；`FLASK_PUBLIC=1` 时必填 |
| `VITE_ADMIN_API_KEY` | 空 | 前端调用 API 时附带的管理密钥（仅适合受控的自托管环境） |
| `JOB_WORKERS` | `2` | 后台任务 worker 数量 |
| `JOB_QUEUE_SIZE` | `24` | 等待中的最大任务数 |
| `FIREFLY_DB_PATH` | `data/firefly.db` | 账号池 SQLite 路径 |
| `LLM_BASE_URL` | `http://127.0.0.1:8317/v1` | 一键成片 LLM 拆镜 base URL |
| `LLM_API_KEY` | `local-dev-key` | 一键成片 LLM Bearer |
| `LLM_MODEL` | `gpt-5.5` | 一键成片 LLM 模型 |
| `LLM_TIMEOUT` | `20` | 一键成片 LLM 超时（秒） |

> `FIREFLY_STORAGE` / `FIREFLY_TOKEN_FILE` / `FIREFLY_TOKEN` / `FIREFLY_OAUTH_TOKEN_FILE` 等老环境变量已不再被读取；
> 账号信息只来自 SQLite `accounts` 表。

---

## 测试

推荐在 `.venv` 环境下运行（系统 Python 通常没装 `edge-tts` 等依赖）：

```bash
# 在仓库根目录
.venv/bin/python tests/test_video_pipeline.py
# 或先激活 venv 再跑
python tests/test_video_pipeline.py
```

只覆盖 `split_storyboard` / `resolve_final_duration` / `_coerce_shot` /
`_validate_video_options` 等纯函数；不调 Firefly 上游，也不打外网 TTS / LLM。
不需要 pytest，直接 `assert` 风格。

CLI smoke（不联网，纯拆分）：

```bash
python video_pipeline.py "首先薄雾升起，然后小鹿出现，最后阳光洒落"
```

## 生产部署

- **本地开发**：`python app.py` + `cd frontend && npm run dev`，全部 127.0.0.1 访问
- **手动 systemd 部署**（ECS / VPS）：见 `manual-deploy.md`
  - `nginx`（:19999）服务前端静态 + 反代 `/api/*` → `gunicorn`（:19998）
  - `firefly-studio.service` 与可选的 `firefly-studio-token.service` 由 systemd 管理
  - 阿里云安全组放行 `19999/TCP` 入站

公网部署时设置 `FLASK_PUBLIC=1` 与 `ADMIN_API_KEY`，并把 Nginx `/api/` 代理中的
`X-Admin-Key` 配成同一个值。前端直连 API（不经过该 Nginx 注入）时，再构建时设置
`VITE_ADMIN_API_KEY`。

---

## 常见问题

- **NameError: name 'os' is not defined**：`app.py` 漏 `import os`。已在 master 修。
- **CORS 跨域**：默认 `*`。生产可设环境变量 `CORS_ORIGINS=https://your-frontend.example`。
- **curl_cffi 没装**：TLS 指纹不像浏览器，会触发 408。`pip install curl_cffi`。

- **408 system under load**：token client_id 与 `x-api-key` 不匹配 / 缺 `x-arp-session-id` / 没装 `curl_cffi` 等。本仓库默认选上传账号里的 client_id 并自动生成 base64 arp。
- **额度耗尽**：上游返回 `401/403` + header `x-access-error=taste_exhausted`，该账号会被自动 cooldown 300s 并切到下一个账号；若全部账号都耗尽，任务会 failed 并显示「上游额度已用完」。
- **playwright 没装浏览器**：`playwright install chromium`（仅在需要 `python token_daemon.py --start` 拿新 token 时才用得到）。
