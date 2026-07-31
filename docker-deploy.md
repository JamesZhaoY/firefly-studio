# Docker 部署 + Cloudflare 转发

## 架构

**单容器架构**（你要求的方案）：Nginx 在 19999 同时服务前端静态 + 反向代理 `/api/*` 到同容器内的 Gunicorn。出口 Adobe 流量走 WARP sidecar。

```
国内用户 ─► 阿里云 ECS :19999 (公网IP)
              │
              ▼
           Docker 容器
              │
              ├─ Nginx :19999
              │    ├─ /              → React 构建产物 (frontend/dist)
              │    └─ /api/*         → Gunicorn :19998 (Flask)
              │                          └─ curl_cffi → WARP sidecar → CF 边缘 → Adobe
              │
              └─ WARP sidecar (caomingjun/warp)
                   └─ 全部出站走 Cloudflare Anycast
```

- 入站端口：**19999**（公网，单一）
- 入站不需要 Cloudflare（你自己评估要不要加 WAF）
- 出站 Adobe 强制走 Cloudflare WARP（绕开国内 ISP 阻断）

> **国内 VPS（阿里云、腾讯云）** → Adobe 直连被墙 / 限速。
> WARP 在容器内零成本解决（详见 §4）。

---

## 1. 本地构建 + 启动

### 1.1 准备 Adobe 凭证

在能跑 Playwright 的机器上：

```powershell
cd D:\workspace\app\adobe
python token_daemon.py --start
# 浏览器 → 解 CAPTCHA → 登录 Adobe
# 自动写 data/storage.json 和 data/current_token.json
```

把这两个 JSON 文件准备好。

### 1.2 用 docker compose 一键启动

```powershell
cd D:\workspace\app\adobe

# 方式 A：环境变量注入（cloud 推荐）
$storage = Get-Content data\storage.json -Raw
$token   = Get-Content data\current_token.json -Raw
# 用 PowerShell 把多行 JSON 转成单行（容器内 CMD 用 printf 写入）
$storageOneLine = $storage -replace "`r`n", "" -replace "`n", ""
$tokenOneLine   = $token   -replace "`r`n", "" -replace "`n", ""

docker compose up -d --build
# 然后在容器外用 PowerShell 注入（也可写在 .env 里）
# 推荐做法：直接写到 .env 文件，避免命令行转义
```

**最稳的方式** —— `.env` 文件：

```bash
# .env（仓库不提交，已 gitignore）
STORAGE_JSON=$(cat data/storage.json)
TOKEN_JSON=$(cat data/current_token.json)
CORS_ORIGINS=https://your-frontend.example.com,https://jameszhaoy.github.io
```

`docker-compose.yml` 自动读取 `.env`。启动：

```bash
docker compose --env-file .env up -d --build
```

### 1.3 验证

```bash
# 容器内 API（Nginx 反代过）
curl http://127.0.0.1:19999/api/health
# → {"ok":true,"auth":{"token_ok":true,"client_id":"clio-playground-web",...}}

# 前端首页
curl -I http://127.0.0.1:19999/
# → HTTP/1.1 200 OK · Content-Type: text/html

# 静态资源
curl -I http://127.0.0.1:19999/assets/index-xxx.js
# → 200 + Cache-Control: public, immutable

# WARP 出口验证
docker compose exec warp warp-cli status    # Connected
docker compose exec app curl https://www.cloudflare.com/cdn-cgi/trace | head
# fl=行最后 4 字符应该是 CF 节点 IP（不是阿里云 IP）
```

查看日志：

```bash
docker compose logs -f app
docker compose logs -f warp
```

---

## 2. 部署到阿里云 ECS（自托管）

### 2.0 ECS 安全组（控制台）

| 方向 | 端口 | 来源 | 说明 |
|------|------|------|------|
| 入 | 19999 | `0.0.0.0/0` 或你的客户端 IP 段 | 前端 + API 共用 |
| 出 | ALL | `0.0.0.0/0` | 让 WARP sidecar 能访问 CF 边缘 |

> 不需要开放 80/443 — 容器自己服务 19999。
> 不需要 CF Tunnel 入站 — CF 只做出口（见 §4）。
> 不想暴露公网 IP，可加 CF Tunnel 反向代理到 19999（见 §3）。

### 2.1 上服务器

```bash
# 本地（Windows）
scp app.py db.py firefly_pipeline.py models_catalog.py wsgi.py \
    requirements.txt Dockerfile docker-compose.yml nginx.conf \
    user@your-ecs:/app/firefly-studio/

# 前端在 docker build 里现编，不需要传 dist/
# 如果要跳过 build 直接用本地构建结果，可：
#   scp -r frontend/dist user@your-ecs:/app/firefly-studio/frontend/

# Adobe 凭证（scp 或 ssh 复制）
ssh user@your-ecs "mkdir -p /app/firefly-studio/data"
scp data/storage.json data/current_token.json \
    user@your-ecs:/app/firefly-studio/data/
# 或者用 .env 文件注入（见下）
```

### 2.2 服务器上启动

```bash
ssh user@your-ecs
cd /app/firefly-studio

# 方式 A：用 .env 文件注入凭证（推荐）
cat > .env <<EOF
STORAGE_JSON='$(cat data/storage.json)'
TOKEN_JSON='$(cat data/current_token.json)'
CORS_ORIGINS=*
EOF

docker compose --env-file .env up -d --build

# 方式 B：凭证放 data/，docker-compose 用 volume 挂载
# 编辑 docker-compose.yml，把 STORAGE_JSON/TOKEN_JSON 留空，
# 并确保 ./data:/data 已挂载 data/storage.json
docker compose up -d --build
```

### 2.3 国内访问

用户直接访问 `http://your-ecs-ip:19999/` 即可：

- `/`  → React 前端
- `/api/*` → Flask 后端

如果要绑域名（推荐，国内访问 IP 不优雅）：

**阿里云 DNS** 解析一条 A 记录到 ECS 公网 IP，然后用 Caddy / nginx 加 HTTPS（容器端口 19999 不支持 HTTPS，需在宿主机加一层）：

```bash
# ECS 上
sudo apt install -y caddy
```

`/etc/caddy/Caddyfile`：

```
firefly.your-domain.com {
    reverse_proxy 127.0.0.1:19999
}
```

```bash
sudo systemctl reload caddy
# → https://firefly.your-domain.com
```

> 不想要 Cloudflare 也能跑。CF 在这套架构里只承担**出站**（去 Adobe），不进站。

### 2.4 验证

```bash
# 前端
curl -I http://your-ecs-ip:19999/
# HTTP/1.1 200 OK · Content-Type: text/html

# API
curl http://your-ecs-ip:19999/api/health

# WARP 出口
docker compose exec warp warp-cli status
# 应输出 "Connected"
```

→ `https://api.your-domain.com/api/health` 自动可用。

---

## 3. Cloudflare 在这套架构里做什么

你说过让 Cloudflare「转发请求」。在当前架构里 Cloudflare 只承担**出站**（去 Adobe）：

```
阿里云 ECS (容器内 curl_cffi) ──► Cloudflare WARP/Anycast ──► Adobe
```

- **不需要** CF Worker / CF Tunnel 入站代理（除非你额外加 WAF）
- **不需要** Cloudflare 域名（除非你想用 CF 的 DDoS 防护）
- Cloudflare 通过 **WARP 客户端** 在容器内提供出站代理

下面 §3.1-§3.4 是你最初提到的可选方案（用 CF Worker 反向代理入站到 ECS），可以作为**额外**的安全层 / WAF 加上去。§4 才是国内 VPS 必走的 WARP 出站方案。

### 3.1 可选：Cloudflare Tunnel / Worker 入站代理

如果你想用 CF 的 WAF / DDoS 防护，可以再加一层：

```
国内用户 ─► Cloudflare Tunnel/Worker ─► ECS :19999
```

安装 cloudflared 到 ECS（不在容器内），建一个 Tunnel 把 `api.your-domain.com` 指向 `http://127.0.0.1:19999`：

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared
chmod +x cloudflared
cloudflared tunnel login
cloudflared tunnel create firefly
cloudflared tunnel route dns firefly firefly.your-domain.com
cloudflared tunnel run firefly
# → https://firefly.your-domain.com
```

ECS 不需要开放 19999 给公网（CF Tunnel 自动建加密隧道）。

```
前端 ─► Cloudflare Worker ─► 你的 VPS :19999 ─► curl_cffi ─► Adobe
```

- Worker 路径示例：`https://api-proxy.your-domain.com/*`
- Worker 收到前端请求 → `fetch("http://your-vps:19999" + path)`
- 你的 VPS 需要 Cloudflare Tunnel 或公网 IP

**Cloudflare Tunnel**（零公网 IP，推荐）：

```bash
# 在 VPS 上
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared
chmod +x cloudflared
cloudflared tunnel login
cloudflared tunnel create firefly
cloudflared tunnel route dns firefly api.your-domain.com
# 启动
cloudflared tunnel run firefly
```

VPS 不暴露任何端口，CF Tunnel 自动建加密隧道。

### 3.2 架构 B：CF Worker 直接转发到 Adobe（绕开 curl_cffi 指纹）

⚠️ **这会有问题**：CF Worker 用标准 fetch，**没有 curl_cffi 的 Chrome TLS 指纹**，Adobe 极可能 408。

如果你确实要这样做：
- 在 Worker 里设置同样的请求头（`x-api-key` / `x-arp-session-id` 等）
- Adobe 部分 API 对 CF 的 datacenter IP 段比较敏感，429 概率上升
- cookie / token 通过 Worker 时需要做好加密切勿泄露

**强烈不推荐**。Adobe 已经验证 curl_cffi 是绕开 408 最稳的方案。

### 3.3 Cloudflare 上要做的事（无论哪种架构）

| 项目 | 操作 |
|------|------|
| **域名 DNS** | 走 CF（橙色云朵），A 记录或 Tunnel |
| **SSL/TLS** | Full (Strict) |
| **WAF 规则** | `/api/*` 路径关闭 Bot Fight / Super Bot Fight Mode（否则前端轮询会被拦） |
| **Rate Limiting** | `/api/generate` 加规则：单 IP 60 req/min（防额度被盗刷） |
| **Access** | 可选：Cloudflare Access 给 `/api/jobs` 等路径加邮箱认证 |
| **Caching** | `/api/models` 加 Edge Cache TTL=300s（模型列表几乎不变） |
| **环境变量** | 在 Worker 或 Page Rules 注入 `CORS_ORIGINS` |

### 3.4 Worker 示例（如果走架构 A）

```js
// Cloudflare Worker: 把前端请求代理到你的后端
export default {
  async fetch(request, env) {
    const url = new URL(request.url)
    // 你的后端可以是 CF Tunnel 域名，也可以是公网 IP
    const backend = env.BACKEND_ORIGIN || 'http://firefly-tunnel.example.ts.net'
    const target = backend + url.pathname + url.search

    const headers = new Headers(request.headers)
    // 让 curl_cffi 看到正确的 host
    headers.set('Host', new URL(backend).host)

    const resp = await fetch(target, {
      method: request.method,
      headers,
      body: request.method === 'GET' || request.method === 'HEAD'
            ? undefined
            : request.body,
    })
    return new Response(resp.body, {
      status: resp.status,
      headers: resp.headers,
    })
  },
}
```

---

## 4. 让 Adobe 请求走 Cloudflare（**国内 VPS 必看**）

### 4.1 为什么需要

阿里云 / 腾讯云 / AWS 北京区等国内 VPS 出网到 Adobe 经常被墙或严重限速（`firefly.adobe.com` / `firefly-3p.ff.adobe.io` / `*.adobe.io` / `*.adobedc.net` 都在 ASN 旁路阻断列表里）。即便容器能跑起来，Adobe 接口也会超时或 408。

### 4.2 方案：在容器里挂 Cloudflare WARP 客户端（推荐 · 免费）

**原理**：WARP 是 Cloudflare 的零配置 VPN 客户端。装在容器里后，所有出站流量经 Cloudflare 边缘节点（全球 Anycast IP），等于从 Cloudflare 内网打 Adobe，**绕过国内 ISP 的 Adobe 阻断**。免费层无流量限制。

### 4.3 实现（修改 docker-compose.yml）

`docker-compose.yml` 加 WARP sidecar：

```yaml
name: firefly-studio

services:
  api:
    build: .
    image: firefly-studio:latest
    container_name: firefly-api
    restart: unless-stopped
    ports:
      - "19999:19999"
    volumes:
      - ./data:/data
      - ./outputs:/app/outputs
    environment:
      PORT: "19999"
      CORS_ORIGINS: "https://your-frontend.example.com"
      STORAGE_JSON: ""
      TOKEN_JSON: ""
    # WARP 把容器全部出站接进 CF 网络
    network_mode: "service:warp"
    depends_on:
      warp:
        condition: service_healthy

  # Cloudflare WARP 客户端（基于 alpine）
  warp:
    image: caomingjun/warp:latest      # ~5MB，含 warp-cli
    container_name: firefly-warp
    restart: unless-stopped
    # 把容器 DNS 设为 CF 的 1.1.1.1，避免国内 DNS 污染
    dns:
      - 1.1.1.1
      - 1.0.0.1
    # WARP 注册/启动（新设备走 registration）
    command: >
      sh -c "
        if [ ! -f /var/lib/cloudflare-warp/reg.json ]; then
          warp-cli registration new && echo 'y' | warp-cli registration license;
        fi;
        warp-cli connect;
        sleep infinity
      "
    volumes:
      - warp-data:/var/lib/cloudflare-warp
    healthcheck:
      test: ["CMD", "warp-cli", "status"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 30s

volumes:
  warp-data: {}
```

> **关键：`api` 容器用 `network_mode: service:warp`**，所有流量都走 warp 容器的网络栈。
> `curl_cffi` 完全不知道中间有代理 — 它发请求 → 流量自动从 warp 容器出去 → 经 CF 到 Adobe。
> 完美保留 Chrome TLS 指纹（这是绕开 408 的核心）。

### 4.4 验证 WARP 工作

```bash
docker compose exec warp warp-cli status
# Connected to Cloudflare WARP+ ...

docker compose exec api curl https://www.cloudflare.com/cdn-cgi/trace
# 看 fl= 行最后 4 位 → 应是 CF 节点的 IP（不是阿里云 IP）

docker compose exec api curl -I https://firefly-adobe-com.awsglobal.something
# 直连测试 Adobe 域名能访问
```

### 4.5 替代方案：CF Tunnel 反向出栈

不想用 WARP，也可以让容器出口走 **Cloudflare Tunnel**，但需要写一个 `cloudflared` 配置把 Adobe 的 SNI 代理出去：

```bash
# cloudflared 在 VPS 上跑（不在容器内）
cloudflared tunnel --hostname api.your-domain.com \
    --url http://localhost:19999
# 再在容器里设 HTTPS_PROXY=http://127.0.0.1:PORT
```

比 WARP 复杂，且对 curl_cffi 不友好。**不推荐**。

### 4.6 替代方案：第三方 Adobe 镜像（极不推荐）

有人说走 `azure-sora` / `fal-veo` 等三方模型替代 Firefly 3P，**但你用的是 Adobe IMS 账号**，绕不开 Adobe 直连。这条路不可行。

---

## 5. 端口汇总

| 服务 | 端口 | 暴露？ | 说明 |
|------|------|--------|------|
| 容器 `app` 内 Nginx | **19999** | 是（公网） | 唯一入站：前端 + `/api/*` |
| 容器 `app` 内 Gunicorn | 19998 | 否（仅 127.0.0.1） | Nginx 反代 |
| 容器 `warp` | - | - | 出站到 CF 边缘 |
| 宿主机 | 19999 | 阿里云安全组开放 | 直连 ECS 公网 IP |
| CF Tunnel（可选入站） | 443 出站 | - | 不需要入站公网 |

---

## 6. 验证清单

```bash
# 本地 / ECS
curl http://127.0.0.1:19999/api/health
curl http://127.0.0.1:19999/api/models
curl -I http://127.0.0.1:19999/                # 200 + text/html
curl -I http://127.0.0.1:19999/assets/index-xxx.js  # 200 + immutable

# 公网（直接连 ECS）
curl http://your-ecs-ip:19999/api/health

# 域名 + Caddy（如果绑域名）
curl https://firefly.your-domain.com/api/health

# CF Tunnel（可选入站代理）
curl https://firefly.your-domain.com/api/health

# WARP 出站验证
docker compose exec warp warp-cli status
docker compose exec app curl https://www.cloudflare.com/cdn-cgi/trace
```

全部应返回 200 + `ok: true`。`cdn-cgi/trace` 的 `fl` 行尾应该是 CF 节点 IP（不是 ECS 的阿里云 IP）。

---

## 7. 故障排查

| 现象 | 原因 | 修复 |
|------|------|------|
| 访问 19999 是 `502 Bad Gateway` | gunicorn 没起来 | `docker compose logs app` 看错误 |
| `/api/health` 返回 503 | `app` 容器还在启动 | 等 20s，或 `docker compose ps` 看 health |
| `auth.token_ok=false` | `storage.json` / `current_token.json` 没注入 | 检查 `.env` 或 volume 挂载 |
| Adobe 408 system under load | 缺 curl_cffi 指纹 / 缺 arp | 镜像里 curl_cffi 已装；arp 由后端自动生成 |
| Adobe 401/403 | token 过期 | `python token_daemon.py --start` 重新登录 |
| Adobe 直连超时 | WARP 没起 / 没注册 | `docker compose exec warp warp-cli status`；首次需注册（容器内自动） |
| `curl: (7) Failed to connect` 容器内到 Adobe | WARP 网络模式问题 | 检查 `network_mode: "service:warp"`；`warp` 健康检查是否通过 |
| CORS 错误 | `CORS_ORIGINS` 没设前端域名 | 改 docker-compose 环境变量 |
| 容器重启数据丢 | 没挂卷 | docker-compose 已配置 `./data:/data` |
| 静态资源 404 | 前端没 build / dist 缺 | `docker compose build --no-cache app` 强制重 build |