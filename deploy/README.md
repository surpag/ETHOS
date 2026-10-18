# ETHOS / EDITH 部署手册（腾讯云 CVM）

> 目标：把 backend-py + web-v1 以 Docker Compose 方式跑在一台云服务器上，Nginx 反代 + HTTPS。  
> 适用范围：单机部署、自用/小规模。多租户水平扩展前请先解决复盘文档里的 P0 项。

---

## 0. 前置条件（缺一不可）

| 项       | 说明                                                                     |
| ------- | ---------------------------------------------------------------------- |
| **域名**  | **必须有**。Clerk 在生产模式要求 HTTPS 域名，纯 IP + HTTP 登录会失败。先去解析一条 A 记录到服务器公网 IP。 |
| 服务器     | 建议 **2 核 4G 起**。2G 内存跑 `next build` 容易 OOM。系统 Ubuntu 22.04/24.04。      |
| 安全组/防火墙 | 放行 **22 / 80 / 443**。8080、3000 不要对公网开放。                                |
| Clerk   | 一个 Clerk 应用，拿到 `pk_*` / `sk_*`；并在 Dashboard 添加你的生产域名。                  |
| E2B     | `E2B_API_KEY` + 模板名（模板不存在会在首次工具调用时报错）。                                 |
| COS     | 存储桶 + SecretId/SecretKey（对话图片用；不用图片功能可留空，但建议配）。                        |

> ⚠️ **安全提醒**：仓库里的 `.env.example` 含真实的 E2B key 与 Clerk secret，属于凭据泄露。部署前请到 E2B / Clerk 后台**轮换这两套密钥**，并且不要把 `.env` 提交进 git。

---

## 1. 服务器初始化

```bash
# 以 Ubuntu 为例，root 或 sudo
apt-get update && apt-get upgrade -y

# 安装 Docker（官方脚本，含 compose 插件）
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker

# 验证
docker --version && docker compose version
```

**2G 内存的机器务必加 swap**（Next 构建需要）：

```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile
mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

设置时区（日志与定时任务用）：

```bash
timedatectl set-timezone Asia/Shanghai
```

---

## 2. 上传代码

在**本地**项目根目录执行（Windows 用 Git Bash / WSL）：

```bash
rsync -avz --progress \
  --exclude 'node_modules' \
  --exclude '.next' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.db' \
  --exclude '*.db-wal' \
  --exclude '*.db-shm' \
  --exclude 'workspace-volumes' \
  --exclude 'workspace-sync' \
  --exclude 'backend-py-tmp' \
  --exclude 'web-v1-tmp' \
  ./ root@<你的服务器IP>:/opt/ethos/
```

> **不要传 `*.db`**：那是你本机数据，传上去会覆盖线上库；首次启动后端会自动建表。  
> 如果项目已在 Git 仓库，也可以直接在服务器 `git clone`，更干净。

服务器上确认关键文件齐全：

```bash
cd /opt/ethos
ls docker-compose.yml backend-py/Dockerfile web-v1/Dockerfile deploy/nginx.conf
```

---

## 3. 配置环境变量

```bash
cd /opt/ethos
cp .env.production.example .env
chmod 600 .env
nano .env          # 填 E2B / COS / Clerk
```

必填：

```ini
EDITH_E2B_TEMPLATE=edith-sandbox-python
E2B_API_KEY=...

EDITH_COS_BUCKET=...
EDITH_COS_REGION=ap-guangzhou
EDITH_COS_SECRET_ID=...
EDITH_COS_SECRET_KEY=...

NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_live_...
CLERK_SECRET_KEY=sk_live_...
```

> `NEXT_PUBLIC_*` 是**构建期内联**的，改了必须 `docker compose build web` 重建，光重启无效。

---

## 4. 首次构建并启动

```bash
cd /opt/ethos
docker compose up -d --build
docker compose ps
```

等待 backend 变为 healthy（首次拉镜像 + 装依赖约 3–8 分钟）：

```bash
docker compose logs -f backend
# 看到 Uvicorn running on http://0.0.0.0:8080 即可
```

本机自测（在服务器上）：

```bash
# 后端直连健康检查（容器内网）
docker compose exec backend curl -fsS http://127.0.0.1:8080/health

# 经 nginx → web 的前端入口，应返回 200/307（307 = 跳登录）
curl -sS -o /dev/null -w 'frontend %{http_code}\n' http://127.0.0.1/
```

`backend` 的健康检查应显示 `healthy`。

---

## 5. 对外入口（A / B 二选一，再配 HTTPS）

| 方案 | 适用场景 | 需要证书 | 需开放 80/443 |
|---|---|---|---|
| **5.A** Nginx + Let's Encrypt（5.1~5.4） | 普通云服务器，能正常访问 80/443、域名已备案 | 是 | 是 |
| **5.B** Cloudflare Tunnel（5.5） | 80/443 被拦截、域名未备案、不想暴露源站 IP | 否 | 否 |

> 若签发证书时报 `unauthorized` / `Error reading HTTP response body: reader size limit exceeded`，
> 说明 80 端口在链路层被拦（未备案域名被云厂商劫持是典型情况）。此时改 nginx 无效，
> 直接改用 **5.5 方案 B**。

### 5.1 先用 HTTP 模式启动（当前 nginx.conf 就是 HTTP 版）

确认 `deploy/nginx.conf` 是 HTTP 版、`deploy/letsencrypt` 目录存在：

```bash
mkdir -p deploy/letsencrypt
docker compose up -d nginx
```

### 5.2 申请证书

#### 方案 A：HTTP-01（webroot）

```bash
docker compose run --rm certbot certonly --webroot -w /var/www/certbot \
  -d your.domain.com \
  --email you@example.com \
  --agree-tos --no-eff-email
```

前提：Let's Encrypt 会从**全球多个视角**同时校验你的 80 端口，任一视角失败即整体失败。
每个视角都必须解析到本机并能读到 `/var/www/certbot/.well-known/acme-challenge/<token>`。

报 `unauthorized` / `Error reading HTTP response body: reader size limit exceeded` 时，
说明链路上有东西挡在前面（未备案域名被云厂商劫持、CDN 代理、GeoDNS 分流、运营商拦截）。
**这种问题改 nginx 是改不好的**，直接用方案 B。

#### 方案 B：DNS-01（Cloudflare API）—— 推荐

DNS 托管在 Cloudflare 时，DNS-01 **完全不需要 Let's Encrypt 访问你的服务器**，
一次性绕开备案拦截 / CDN / 防火墙 / GeoDNS / 80 端口封锁的所有问题。

1. Cloudflare Dashboard → My Profile → **API Tokens** → Create Token
   - 权限：`Zone → DNS → Edit`、`Zone → Zone → Read`
   - Zone Resources：选中该域名所在的 zone
2. 写凭据文件（**不要提交进 git**）：

```bash
echo 'dns_cloudflare_api_token = 你的token' > deploy/cloudflare.ini
chmod 600 deploy/cloudflare.ini
```

3. 签发（官方 `certbot/dns-cloudflare` 镜像自带插件）：

```bash
docker run --rm \
  -v "$PWD/deploy/letsencrypt:/etc/letsencrypt" \
  -v "$PWD/deploy/cloudflare.ini:/cloudflare.ini:ro" \
  certbot/dns-cloudflare certonly \
  --dns-cloudflare \
  --dns-cloudflare-credentials /cloudflare.ini \
  --dns-cloudflare-propagation-seconds 30 \
  -d your.domain.com --email you@example.com --agree-tos --no-eff-email
```

4. 续期也必须用同一个镜像（`certbot/certbot` 没装该插件）：

```bash
docker run --rm \
  -v /opt/ethos/deploy/letsencrypt:/etc/letsencrypt \
  -v /opt/ethos/deploy/cloudflare.ini:/cloudflare.ini:ro \
  certbot/dns-cloudflare renew --quiet
docker compose exec nginx nginx -s reload
```

成功后会生成 `deploy/letsencrypt/live/your.domain.com/`。

### 5.3 切到 HTTPS 配置

```bash
# 把 https 模板里的 example.com 全部替换成你的域名
sed 's/example\.com/your.domain.com/g' deploy/nginx.https.conf > deploy/nginx.conf
docker compose restart nginx
```

访问 `https://your.domain.com` 验证。

### 5.4 证书自动续期（可选但建议）

```bash
cat > /etc/cron.d/certbot-renew <<'EOF'
0 3 * * * root cd /opt/ethos && docker compose run --rm certbot renew --quiet && docker compose restart nginx
EOF
```

> 上面是 **HTTP-01** 的续期命令。若用 **DNS-01** 签发，必须换成 `docker run ... certbot/dns-cloudflare renew`
> （见 5.2 方案 B 第 4 步），否则 `certbot/certbot` 镜像里没有 cloudflare 插件，续期会静默失败。

### 5.5 方案 B：Cloudflare Tunnel（无需证书、无需开放端口）

**适用**：云厂商对未备案域名的 80/443 做拦截、机器没有公网入口、或不想暴露源站 IP。

**原理**：服务器**主动出站**连 Cloudflare 建立隧道，用户访问的是 CF 边缘节点，由 CF 提供受信 HTTPS 证书。
**入站流量不经过本机端口**，因此端口拦截、安全组、备案都影响不到它，也完全不需要 certbot。

1. Cloudflare Dashboard → **Zero Trust → Networks → Tunnels → Create a tunnel**，选 `Cloudflared`，起名后拿到 token。
2. 在该 tunnel 的 **Public Hostname → Add a published application** 里配置：

   | 字段 | 填什么 |
   |---|---|
   | Subdomain | 子域名，如 `surpag` |
   | Domain | 主域名 |
   | **Path** | 留空 |
   | **Service URL** | `http://nginx:80`（**必须带 `http://` 前缀**，这是 origin 地址） |
   | Additional settings → **HTTP Host Header** | **留空 / 恢复默认**（CF 会自动传 `Host: 你的域名`） |

   > 最常见的填错：把 `nginx:80` 填进 **HTTP Host Header** → origin 收到的 Host 变成 `nginx:80`，
   > Clerk 计算回调地址就会出错。origin 地址永远填在 **Service URL**。

   若 nginx 已经是 **HTTPS 版**（80 端口配了 `return 301 https://...`），隧道再指向 `http://nginx:80`
   会形成**无限重定向**（`ERR_TOO_MANY_REDIRECTS`）。此时二选一：
   - 恢复 HTTP 版 `deploy/nginx.conf` 后重启 nginx；或
   - **直接指向 `http://web:3000`**，绕过 nginx（最省事，代价是不再走 nginx 的上传体积限制）。

   > 保存 Public Hostname 时 CF 会自动建一条 CNAME 记录。若该子域名下已存在 A/AAAA 记录，
   > 会报「记录已存在」——**先在 DNS 里删掉旧的 A 记录**再保存。

3. token 写入 `.env`：

```bash
echo 'CLOUDFLARE_TUNNEL_TOKEN=eyJ...' >> .env
```

4. 启动（`tunnel` 服务在独立 profile 下，不会随默认 `up` 启动）：

```bash
docker compose --profile tunnel up -d tunnel
docker compose logs -f tunnel     # 出现 "Registered tunnel connection" 即成功
```

5. 浏览器访问 `https://你的域名`。

**注意**：

- `deploy/nginx.conf` **保持 HTTP 版**，不需要执行 5.2 的 certbot 步骤；安全组里的 80/443 可以关闭。
- 隧道默认走 QUIC（UDP 7844），出网受限时会连不上，compose 里已固定 `--protocol http2`（走 TCP 443）。
- `NEXT_PUBLIC_CLERK_*` 仍是**构建期**变量，改完要 `docker compose build web`，只 restart 无效。
- 流量经 Cloudflare，国内访问速度取决于 CF 节点；SSE 一般可用，若发现输出被攒批，检查 CF 是否开了压缩/缓存规则。
- 只适合自用 / 团队内验证。按所在地法规，面向公众提供服务仍需完成备案。

---

## 6. Clerk 生产配置

在 Clerk Dashboard：

1. **Domains**：添加 `your.domain.com`，按提示配置 DNS（CNAME）。
2. **Paths**：确保 `/sign-in`、`/sign-up` 与 `.env` 中的 `NEXT_PUBLIC_CLERK_*` 一致。
3. 如需限制注册，开启 **Restrictions**（自用建议关闭公开注册）。

> 若暂时用 `pk_test_*`，Clerk 会以 development 实例运行（有用户数/域名限制）；正式使用建议切 production 实例并重建 web 镜像。

---

## 7. 上线验证清单

- [ ] `docker compose ps` 中 backend 为 `healthy`，web / nginx 为 `running`
- [ ] `https://域名/` 能打开并跳转登录
- [ ] 能完成 Clerk 登录（若失败，先查 Clerk Domains 与 HTTPS）
- [ ] 设置页能保存并测试模型（说明后端 → 模型供应商出网正常）
- [ ] 新建工作区并上传一个文件（说明 SQLite + workspace-volumes 正常）
- [ ] 发一条消息，Agent 能调工具（说明 E2B 出网与模板存在）
- [ ] 消息流式输出不卡顿（说明 nginx `proxy_buffering off` 生效）

---

## 8. 日常运维

### 数据在哪里

全部在命名卷 `ethos_edith-data`（容器内 `/app/data`）：

```
/app/data/edith-v2.db          # SQLite 全部业务数据
/app/data/workspace-volumes/   # 工作区文件（local 后端）
/app/data/workspace-sync/      # 双向同步共识状态
```

### 备份（建议每天）

```bash
mkdir -p /opt/backup
docker run --rm -v ethos_edith-data:/data -v /opt/backup:/backup alpine \
  tar czf /backup/ethos-$(date +%F).tar.gz -C /data .
# 保留最近 14 天
find /opt/backup -name 'ethos-*.tar.gz' -mtime +14 -delete
```

> SQLite 是单文件，**必须停服或热备**。上面的 tar 方式在写入瞬间可能不一致；严谨做法：  
> `docker compose exec backend sqlite3 /app/data/edith-v2.db ".backup /app/data/backup.db"` 后再打包。

### 更新版本

```bash
cd /opt/ethos
# 上传新代码后
docker compose up -d --build
docker image prune -f
```

### 看日志

```bash
docker compose logs -f backend
docker compose logs -f web
docker compose logs -f nginx
```

---

## 9. 常见问题

| 现象            | 原因                 | 处理                                                                  |
| ------------- | ------------------ | ------------------------------------------------------------------- |
| 页面 502        | web 未起 / 构建失败      | `docker compose logs web`；多为 `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` 未传 |
| 登录后 401 / 一直转 | Clerk 域名未配或非 HTTPS | 检查 Clerk Domains、证书是否生效                                             |
| 设置页「无法加载设置」   | 后端 500（DB 锁 / 未起）  | `docker compose logs backend`；确认只有一个 backend 实例                     |
| 发消息无响应、卡住     | SSE 被缓冲            | 确认 nginx `proxy_buffering off` 已生效并 reload                          |
| Agent 工具全失败   | E2B key / 模板名错     | 核对 `E2B_API_KEY`、`EDITH_E2B_TEMPLATE`                               |
| 上传文件 413      | nginx 体积限制         | 已设 `client_max_body_size 100m`，按需调大                                 |
| 构建 OOM        | 内存不足               | 加 swap 或升到 4G                                                       |
| 重启后数据没了       | 卷被误删               | 检查 `docker volume ls` 是否有 `ethos_edith-data`；恢复备份                   |

### 地域与网络（国内节点必读）

Clerk 与 E2B 都在海外，**国内节点访问它们可能慢或不稳定**，这直接决定体验：

```bash
# 在服务器上先探测，再决定是否换地域
curl -o /dev/null -sS -w 'clerk %{http_code} %{time_total}s\n' https://clerk.com
curl -o /dev/null -sS -w 'e2b   %{http_code} %{time_total}s\n' https://api.e2b.dev
```

- 两个都通但 `time_total > 2s`：能跑，但 Agent 工具调用会明显变慢（沙箱在海外）。
- Clerk 不通：登录直接不可用 → 建议换**中国香港/新加坡**节点，或给服务器配出网代理。
- E2B 慢是主要体感来源（本机实测单次文件读取 2–8.8s），换地域比调参数有效。

---

## 10. 本地预验证（可选，建议做）

在把镜像推到服务器前，本机启动 Docker Desktop 后可以先用**同一套文件**验证构建：

```bash
cd /path/to/EDITH
docker compose build            # 只验证能不能构建，不启动
docker compose up -d            # 本地访问 http://localhost
```

> 构建阶段就能暴露 90% 的部署问题（缺依赖、锁文件不一致、NEXT_PUBLIC 未传）。  
> 注意 `docker-compose.yml` 里 nginx 占 80/443，本机若已被占用，先改端口映射。

---

## 附：不用 Docker 的裸机方案（不推荐，仅备选）

```bash
# 后端
cd /opt/ethos/backend-py
python3.13 -m venv .venv && . .venv/bin/activate
pip install uv && uv sync --no-dev
EDITH_ADDR=127.0.0.1:8080 EDITH_DB_PATH=/opt/ethos/data/edith-v2.db \
  uvicorn edith_backend.main:app --host 127.0.0.1 --port 8080
# 用 systemd 托管，前端 npm ci && npm run build && npm start
```

裸机方案需要自己处理 Python 3.13、Node 22、进程守护、日志轮转，**故障面明显大于 Docker**，仅在没有 Docker 权限时使用。
