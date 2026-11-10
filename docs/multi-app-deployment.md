# 单服务器多应用部署（子域名分流）

> 场景：同一台服务器再部署第二个应用，与现有 ETHOS 共存，各自独立域名。
> 前提：ETHOS 当前经 Cloudflare Tunnel 对外服务（`surpag.de5.net` → `http://nginx:80`）。

## 1. 先明确：分流发生在两层，不是一层

因为流量是 **cloudflared 主动出站** 拉进来的，不是从本机 80/443 入站，所以：

| 层 | 位置 | 依据 | 作用 |
|---|---|---|---|
| 第一层 | Cloudflare 边缘 | Public Hostname（子域） | 决定走哪条 ingress 到隧道 |
| 第二层 | nginx 容器 | `server_name`（Host 头） | 决定转发给哪个上游容器 |

两层都配了才通。只配 CF 不配 nginx → 落到默认站点；只配 nginx 不配 CF → 域名解析不到。

```
app2.de5.net ─┐
              ├→ CF 边缘(TLS) → cloudflared → nginx:80 ─┬→ web:3000   (ETHOS)
surpag.de5.net┘                                          └→ app2:3000  (新应用)
```

## 2. 先做两个决策

### 决策 A：第二个应用放哪

| 方式 | 适用 | 代价 |
|---|---|---|
| 并入 ETHOS 的 `docker-compose.yml` | 自己维护、和 ETHOS 一起迭代 | 耦合，一次 `up -d` 会一起动 |
| **独立 compose stack + 共享网络**（推荐） | 独立仓库、独立发布节奏 | 需要手动建一个 external network |

推荐后者：故障域隔离，app2 挂了不影响 ETHOS。

### 决策 B：真的还要 nginx 吗

理论上可以让 CF 的 Public Hostname 直接指向 app2 容器（少一跳）。但**建议保留 nginx**：

- 入口策略集中一处（SSE 关闭缓冲、body size、安全头），不用每个应用各写一份
- 将来域名备案完成、切回直连 80/443 时，nginx 就是现成入口
- cloudflared 容器无需加入 app2 的网络

## 3. 操作步骤

以下把新应用记作 `app2`，容器内端口记作 `3000`，实际替换。

### 3.0 先备份（改错会让 ETHOS 一起挂）

```bash
cd /opt/ethos/ETHOS
cp deploy/nginx.conf deploy/nginx.conf.bak
```

### 3.1 建共享网络

```bash
docker network create edge
```

已存在会报 `already exists`，忽略即可。

### 3.2 ETHOS 侧：让 nginx 加入 edge

`docker-compose.yml` 的 `nginx` 服务加网络，文件末尾加 networks 段：

```yaml
  nginx:
    image: nginx:1.27-alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./deploy/nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - ./deploy/letsencrypt:/etc/letsencrypt:ro
      - certbot-webroot:/var/www/certbot:ro
    depends_on:
      - web
    restart: unless-stopped
    networks:
      - default
      - edge          # 新增

networks:
  edge:
    external: true    # 新增
```

`default` 是 compose 自动建的，必须显式写上，否则加了 `networks` 就只剩 `edge`，nginx 就解析不到 `web` 和 `backend` 了。

### 3.3 app2 侧：起容器并加入 edge

```yaml
name: app2

services:
  app2-web:                 # 注意：不要叫 web，见 §4.1
    build: .
    restart: unless-stopped
    volumes:
      - app2-data:/app/data
    networks:
      - default
      - edge
    # 不要写 ports: 80/443，宿主机端口已归 nginx

volumes:
  app2-data:

networks:
  edge:
    external: true
```

**顺序很重要：先把 app2 起起来，再改 nginx。**

```bash
cd /opt/app2
docker compose up -d
docker compose ps          # 确认状态是 running
```

### 3.4 改 nginx.conf 为多站点

关键改动三处：① 兜底块标 `default_server` ② 补 `client_max_body_size` ③ `Connection` 改用 map（原写法会让 WebSocket 握手失败）。

```nginx
# 上游是 Cloudflare Tunnel 时带 X-Forwarded-Proto: https；直连时回落 $scheme
map $http_x_forwarded_proto $forwarded_proto {
    default $http_x_forwarded_proto;
    ""      $scheme;
}

# WebSocket 升级：原配置写 Connection "" 会清空该头，导致 WS 握手失败
map $http_upgrade $connection_upgrade {
    default upgrade;
    ""      close;
}

# ========== 默认站点：ETHOS ==========
server {
    listen 80 default_server;          # 必须显式标，否则依赖文件顺序
    server_name surpag.de5.net _;

    client_max_body_size 100m;         # nginx 默认 1MB，工作区传文件会 413

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        proxy_pass http://web:3000;
        proxy_http_version 1.1;

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $forwarded_proto;
        proxy_set_header Upgrade           $http_upgrade;
        proxy_set_header Connection        $connection_upgrade;

        # SSE：不缓冲 + 长超时
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        chunked_transfer_encoding on;
    }
}

# ========== 第二个应用 ==========
server {
    listen 80;
    server_name app2.de5.net;

    client_max_body_size 50m;

    location / {
        proxy_pass http://app2-web:3000;   # 与 §3.3 的 service 名一致
        proxy_http_version 1.1;

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $forwarded_proto;
        proxy_set_header Upgrade           $http_upgrade;
        proxy_set_header Connection        $connection_upgrade;

        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

若 app2 也用 SSE / 流式输出，把 `proxy_buffering off; proxy_cache off;` 一并复制过去，否则前端会「卡住到整段结束才显示」。

### 3.5 验证并热加载

```bash
docker compose exec nginx nginx -t          # 必须先过这步
docker compose exec nginx nginx -s reload
```

`nginx -t` 报 `host not found in upstream "app2-web"` = app2 容器没起来或不在 edge 网络，回到 §3.3 检查。**不要在这个状态下 reload**，也不要去 `restart` nginx——重启会因解析失败直接起不来，ETHOS 一起挂。

### 3.6 Cloudflare 侧加域名

顺序不能反（否则会复现上次那个「CNAME 没自动建」的坑）：

1. **先看 DNS**：`app2.de5.net` 若已存在 A 记录，**先删掉**。A 记录还在时保存 Public Hostname，CF 只登记 ingress、不建 DNS。
2. **再在 Tunnel → Public Hostname 加一条**：

| 字段 | 值 |
|---|---|
| 子域 | `app2` |
| 域 | `de5.net` |
| 服务 | `http://nginx:80` |
| HTTP Host Header | **留空**（上次填 `nginx:80` 导致 Host 被覆盖） |

3. 回 DNS 页确认已自动生成 `app2` → `58eee9eb-....cfargotunnel.com` 的 **CNAME**，代理状态必须 **Proxied（橙云）**。

**两条 Public Hostname 都指向 `http://nginx:80`**，不要一条指 nginx、一条直连 app2——后者会绕过统一入口策略，且 cloudflared 未必解析得到 app2 容器名。

### 3.7 端到端验证

```bash
curl -I https://app2.de5.net          # 期望 200/302
curl -I https://surpag.de5.net        # 确认老站没坏
docker compose logs --tail=50 tunnel  # 看 ingress 是否更新
```

## 4. 六个必须知道的坑

### 4.1 两个 stack 的服务名不能重名（最隐蔽）

ETHOS 有 `web`，如果 app2 也叫 `web`，两个容器在 `edge` 网络里共享同一个 DNS 名。nginx 的 `proxy_pass http://web:3000` 会在两者之间**轮流解析**——表现是「偶尔打到另一个应用」，不报错、不崩，极难排查。

对策：app2 的 service 名用唯一前缀（`app2-web`），或用显式别名：

```yaml
    networks:
      edge:
        aliases:
          - app2
```

### 4.2 `proxy_pass` 的主机名是启动时解析的

上游容器不存在 → nginx 启动失败。所以顺序必须是「先起上游，再 reload nginx」。这也是为什么不能提前把 app2 的 server 块提交到主分支让 CI 自动部署——会炸掉 ETHOS。

### 4.3 现有配置缺 `client_max_body_size`

`nginx.conf`（线上挂载的那份）**没有**这个指令，nginx 默认 1MB。ETHOS 的工作区文件上传超过 1MB 会被 413 直接拒掉；`nginx.https.conf` 里写了 100m，但那份没在用。§3.4 已补，建议一并上线。

### 4.4 `Connection ""` 让 WebSocket 握手失败

原配置 `proxy_set_header Connection "";` 会清空 Connection 头。SSE 不受影响（普通 HTTP 长响应），但 WebSocket 需要 `Connection: Upgrade`。若 app2 用 WS（实时协作、通知推送），必须换成 §3.4 的 `$connection_upgrade` map。

### 4.5 宿主机端口只能有一个主人

nginx 已占 80/443。app2 自己的 compose 里**不要写 80/443 映射**。容器内端口随意（3000/8000/8080），通过 edge 网络访问即可。

### 4.6 备份脚本不覆盖 app2

`deploy/backup.sh` 只备 `edith-data` 卷。app2 的数据卷要另外进备份计划，并且**同样不能用 `cp` 拷 SQLite 文件**（WAL 模式下最近的写入还在 `-wal` 里，必须用 `sqlite3.Connection.backup()`）。

## 5. 上线前检查清单

- [ ] `docker network ls | grep edge` 存在
- [ ] `docker compose exec nginx nginx -t` 通过
- [ ] `curl -I https://surpag.de5.net` 仍正常（老站回归）
- [ ] `curl -I https://app2.de5.net` 返回 app2 的内容，不是 ETHOS 的首页
- [ ] CF DNS 里 `app2` 是 CNAME + 橙云，不是 A 记录
- [ ] 两个域名交叉测试：`curl -I -H "Host: app2.de5.net" http://<服务器IP>/` 应返回 app2（验证 server_name 分流本身正确，排除 CF 层干扰）
- [ ] 服务器内存余量 ≥ 2GB（现 4 容器 + 新应用）

## 6. 之后加第三个应用

改动只有三处，且都不碰已有站点：

1. app2 的 compose 里多一个 service，也在 `edge` 网络
2. `nginx.conf` 多一个 `server` 块（`listen 80;` 不加 `default_server`）
3. CF 加一条 Public Hostname → `http://nginx:80`

`default_server` 只允许一个，后续所有块都不要写。
