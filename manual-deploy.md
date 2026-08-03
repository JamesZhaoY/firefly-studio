# 手动部署（systemd + nginx）

不再使用 Docker。在阿里云 ECS / Ubuntu 22.04+ 上以 `root` 或具备 `sudo` 权限的账户执行。

## 0. 准备工作

放行入站 `19999/TCP`：

- 阿里云 ECS 控制台 → 安全组 → 入方向 → 放行 `19999/19999`，来源 `0.0.0.0/0` 或限定你的客户端 IP 段。

安装依赖：

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv \
                    nginx nodejs npm ffmpeg
# playwright 用于登录拿 Adobe cookie
pip3 install --break-system-packages playwright
playwright install chromium
```

---

## 1. 部署目录

```bash
sudo mkdir -p /opt/firefly-studio
sudo chown $USER /opt/firefly-studio
cd /opt/firefly-studio
```

把仓库拷到 `/opt/firefly-studio`（任选其一）：

```bash
# 方案 A：git clone
git clone https://github.com/JamesZhaoY/firefly-studio.git .

# 方案 B：scp（在本机执行）
scp -r app.py db.py firefly_pipeline.py models_catalog.py \
        video_pipeline.py wsgi.py requirements.txt nginx.conf \
        frontend root@<ECS_IP>:/opt/firefly-studio/
```

建好运行时目录：

```bash
mkdir -p data outputs logs
```

---

## 2. Python 依赖

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
```

确认 `ffmpeg` 可用（前端一键成片会调用）：

```bash
ffmpeg -version
```

---

## 3. Adobe 凭证

首次或 cookie 失效时：

```bash
. .venv/bin/activate
python token_daemon.py --start
```

浏览器弹出 → 在 `firefly.adobe.com` 解 CAPTCHA + 登录 → 自动写 `data/storage.json` 和 `data/current_token.json`。`Ctrl+C` 退出。

后续后台自动刷新：

```bash
python token_daemon.py --run &     # 或使用 systemd，见 §6
```

---

## 4. 构建前端

```bash
cd frontend
npm install
npm run build         # 产物在 frontend/dist
cd ..
```

`nginx.conf` 期望静态文件位于 `/opt/firefly-studio/frontend/dist`。如果部署到其他路径，修改 `nginx.conf` 的 `root`：

```nginx
root /your/path/firefly-studio/frontend/dist;
```

---

## 5. nginx 反向代理

```bash
sudo cp nginx.conf /etc/nginx/sites-available/firefly-studio
sudo ln -sf /etc/nginx/sites-available/firefly-studio /etc/nginx/sites-enabled/firefly-studio
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

验证：

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:19999/healthz   # 期望 200
```

---

## 6. systemd 服务

`/etc/systemd/system/firefly-studio.service`：

```ini
[Unit]
Description=Firefly Studio (Gunicorn)
After=network.target

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=/opt/firefly-studio
Environment="PATH=/opt/firefly-studio/.venv/bin"
Environment="FLASK_HOST=127.0.0.1"
Environment="FLASK_PORT=19998"
Environment="PORT=19998"
Environment="CORS_ORIGINS=https://your-frontend.example,https://jameszhaoy.github.io"
ExecStart=/opt/firefly-studio/.venv/bin/gunicorn \
    -w 1 -k gthread --threads 4 --timeout 600 \
    --bind 127.0.0.1:19998 \
    --access-logfile /opt/firefly-studio/logs/access.log \
    --error-logfile /opt/firefly-studio/logs/error.log \
    wsgi:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

如果 `data/`、`outputs/` 需要 `www-data` 写入：

```bash
sudo chown -R www-data:www-data /opt/firefly-studio/data /opt/firefly-studio/outputs /opt/firefly-studio/logs
```

启用并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now firefly-studio
sudo systemctl status firefly-studio --no-pager
```

后台 token 刷新（可选，独立单元）：

```ini
# /etc/systemd/system/firefly-studio-token.service
[Unit]
Description=Firefly Studio Token Daemon
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/firefly-studio
ExecStart=/opt/firefly-studio/.venv/bin/python token_daemon.py --run
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now firefly-studio-token
```

---

## 7. 验证

```bash
# API 健康
curl http://127.0.0.1:19999/api/health
# 前端首页
curl -I http://127.0.0.1:19999/
# 公网（替换为你的 ECS 公网 IP）
curl http://<ECS_PUBLIC_IP>:19999/api/health
```

期望返回 200 + JSON。

---

## 8. 出口网络（Adobe）

服务通过本机出口访问 `firefly-3p.ff.adobe.io`、`adobeid-na1.services.adobe.com`、`*.bks*.adobe.io`、Edge TTS 等域名。若 ECS 直接访问被阻断，需要在宿主机配置代理（`HTTP_PROXY` / SOCKS5）或 TUN。

**应用层代理（推荐）**：启动 gunicorn 前设置：

```bash
Environment="HTTP_PROXY=http://127.0.0.1:7890"
Environment="HTTPS_PROXY=http://127.0.0.1:7890"
Environment="NO_PROXY=127.0.0.1,localhost"
```

**TUN 模式**：在宿主机跑 Mihomo / Clash Meta TUN，无需改应用。

若走 TUN，回程流量可能干扰 ECS 公网入站，必要时配置 TUN 的 `route-exclude-address` 排除 ECS 公网 IP / docker bridge（本部署未使用 Docker，但仍要排除 VPS 内网网段）。

---

## 9. 常见问题

- **端口被占用**：修改 `nginx.conf` 中 `listen 19999` 与 `proxy_pass http://127.0.0.1:19998`，同步修改 `firefly-studio.service` 的 `--bind`。
- **公网不通**：检查阿里云安全组入方向、`sudo nginx -t`、`sudo systemctl status nginx`、`sudo journalctl -u firefly-studio -n 100`。
- **凭据 408 / 401**：重新跑 `python token_daemon.py --start`；确保 `curl_cffi` 已装。
- **端口转发冲突**：若同时跑 `clashx`、`mihomo` 等 TUN 客户端，确认其 `mixed-port` 与本服务不冲突。