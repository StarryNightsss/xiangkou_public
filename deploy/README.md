# 部署说明（生产）

一套 compose 起 5 个服务：`mysql` / `redis` / `go-api`（Go 点评后端）/ `app`（Agent + 前端）/ `caddy`（可选 HTTPS）。
只有 `app` 对外暴露（默认绑 `127.0.0.1:8090`），数据库和 Redis 只在容器网络里。

## 本机先验证

```powershell
cp deploy/.env.example deploy/.env    # 填 MYSQL_ROOT_PASSWORD / REDIS_PASSWORD 等
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env up -d --build
curl http://127.0.0.1:8090/health
```

想换端口：在 `deploy/.env` 里设 `APP_PORT=18090`。

## 服务器首次部署

```bash
# 1. 装 Docker（Ubuntu 示例）
curl -fsSL https://get.docker.com | sh

# 2. 拉代码
git clone https://github.com/StarryNightsss/xiangkou.git ~/xiangkou
cd ~/xiangkou

# 3. 配环境变量
cp deploy/.env.example deploy/.env && vim deploy/.env   # 填密码、域名、模型 key、SMTP

# 4. 起服务（首次会构建镜像，几分钟）
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env up -d --build

# 5. 需要 HTTPS 时启用 Caddy（自动申请证书）
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env --profile tls up -d
```

域名解析到服务器后，`DOMAIN` 和 `PUBLIC_BASE_URL` 都填正式域名，Caddy 会自动签发证书。

## GitHub Actions 自动部署（可选）

`.github/workflows/deploy.yml`：打 `v*` tag 或手动触发时，构建镜像推到 GHCR，再 SSH 到服务器拉取重启。

启用前在仓库 Settings 里配置：

| 类型 | 名称 | 说明 |
|---|---|---|
| Variable | `DEPLOY_ENABLED` | 设成 `true` 才开始部署 job |
| Variable | `PUBLIC_HOST` | 冒烟测试用，例如 `xiangkou.example.com` |
| Secret | `DEPLOY_HOST` | 服务器 IP / 域名 |
| Secret | `DEPLOY_USER` | SSH 用户 |
| Secret | `DEPLOY_SSH_KEY` | 私钥（对应公钥放服务器 `~/.ssh/authorized_keys`） |

服务器上需要 `~/xiangkou/deploy/.env` 已存在（workflow 只传 compose 文件，不传密钥）。

## 数据与备份

| 数据 | 位置 |
|---|---|
| MySQL | 命名卷 `xiangkou_mysql-data` |
| Redis | 命名卷 `xiangkou_redis-data`（AOF） |
| Agent 会话 | 命名卷 `xiangkou_app-data`（SQLite） |
| 上传图片 | 命名卷 `xiangkou_uploads` |

```bash
# 每天备份（crontab 示例）
docker compose -f ~/xiangkou/deploy/docker-compose.prod.yml exec -T mysql \
  mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" xiangkou | gzip > /backup/xiangkou-$(date +%F).sql.gz
```

## 邮件与对账

```text
SMTP      用 SPRING_MAIL_USERNAME / SPRING_MAIL_PASSWORD（也可用 SMTP_* 覆盖）
          465 走隐式 TLS，587 走 STARTTLS；不配置时验证码只写 Redis
对账      RECONCILE_INTERVAL_SECONDS（默认 300）：
          Redis 库存向 MySQL 收敛；孤儿「一人一单」成员移除、缺失成员补齐
```

## 发布新版本（单机、国内无法拉 Docker Hub 时）

```powershell
$env:$env:XK_SSH_PASS="服务器密码"
$env:XK_MYSQL_DSN="xk:密码@tcp(127.0.0.1:3306)/xiangkou?parseTime=true&charset=utf8mb4&loc=Local"
python scripts/deploy_remote.py --verify   # 全量
python scripts/deploy_remote.py --go       # 只更新 go-api
python scripts/deploy_remote.py --app      # 只更新 app
```

脚本流程：本地 `docker build` → 打包 → SFTP 上传 → `docker load` → 只重启对应容器 → 探活。

## 注意

- **不要 `down -v`**：会清空数据库、Redis、会话和上传图片。
- 首次启动的数据库由 `01-schema.sql` + `02-seed-data.sql` 初始化，之后改种子不会自动重导。
- 生产建议先加登录/发码限流，再公开域名（当前只做了 Origin 校验）。
