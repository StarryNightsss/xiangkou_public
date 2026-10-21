#!/usr/bin/env bash
# 一键部署：在全新的 Ubuntu/Debian 服务器上装 Docker、拉代码、生成配置、起栈、探活。
# 用法：
#   DOMAIN=example.com bash deploy/bootstrap.sh
#   REPO_URL=git@github.com:you/xiangkou.git DOMAIN=example.com bash deploy/bootstrap.sh
# 不带 DOMAIN 时只起本机端口（适合先验收），之后补 DOMAIN 再跑一次即可开 HTTPS。
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/StarryNightsss/xiangkou.git}"
BRANCH="${BRANCH:-main}"
DIR="${DIR:-/opt/xiangkou}"
DOMAIN="${DOMAIN:-}"
APP_PORT="${APP_PORT:-8090}"
LLM_PROVIDER="${XIANGKOU_LLM_PROVIDER:-deepseek}"

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

# 1. 依赖
if ! command -v docker >/dev/null 2>&1; then
  log "安装 Docker"
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi
if ! docker compose version >/dev/null 2>&1; then
  warn "缺少 docker compose 插件，请先安装 docker-compose-plugin"
  exit 1
fi
command -v git >/dev/null 2>&1 || { log "安装 git"; apt-get update -y && apt-get install -y git curl; }

# 2. 代码
if [ -d "$DIR/.git" ]; then
  log "更新代码 $DIR"
  git -C "$DIR" fetch --all --prune
  git -C "$DIR" checkout "$BRANCH"
  git -C "$DIR" pull --ff-only
else
  log "克隆到 $DIR"
  mkdir -p "$(dirname "$DIR")"
  git clone --branch "$BRANCH" "$REPO_URL" "$DIR"
fi

# 3. 配置：已存在就不覆盖
ENV_FILE="$DIR/deploy/.env"
if [ ! -f "$ENV_FILE" ]; then
  log "生成 deploy/.env"
  MYSQL_ROOT_PASSWORD="$(openssl rand -hex 16)"
  REDIS_PASSWORD="$(openssl rand -hex 16)"
  cat > "$ENV_FILE" <<EOF
MYSQL_ROOT_PASSWORD=$MYSQL_ROOT_PASSWORD
REDIS_PASSWORD=$REDIS_PASSWORD
PUBLIC_BASE_URL=${DOMAIN:+https://$DOMAIN}${DOMAIN:-http://127.0.0.1:$APP_PORT}
DOMAIN=${DOMAIN:-localhost}
APP_PORT=$APP_PORT
XIANGKOU_LLM_PROVIDER=$LLM_PROVIDER
XIANGKOU_LLM_MODEL=${XIANGKOU_LLM_MODEL:-}
XIANGKOU_LLM_API_KEY=${XIANGKOU_LLM_API_KEY:-}
XIANGKOU_LLM_BASE_URL=${XIANGKOU_LLM_BASE_URL:-}
XIANGKOU_CATALOG_ADMIN_IDS=${XIANGKOU_CATALOG_ADMIN_IDS:-1010,1012}
EOF
  chmod 600 "$ENV_FILE"
  warn "已生成随机密码；模型 Key 请补到 $ENV_FILE 后重新执行本脚本"
else
  log "保留已有 $ENV_FILE"
  if [ -n "$DOMAIN" ]; then
    sed -i "s|^DOMAIN=.*|DOMAIN=$DOMAIN|" "$ENV_FILE"
    sed -i "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=https://$DOMAIN|" "$ENV_FILE"
  fi
fi

# 4. 起栈
cd "$DIR"
PROFILE_ARGS=()
if [ -n "$DOMAIN" ] && [ "$DOMAIN" != "localhost" ]; then
  PROFILE_ARGS=(--profile tls)
  log "启用 Caddy 自动 HTTPS：$DOMAIN"
else
  warn "未设置 DOMAIN，只监听 127.0.0.1:$APP_PORT（不签证书）"
fi

log "构建并启动容器"
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env "${PROFILE_ARGS[@]}" up -d --build

# 5. 探活
log "等待服务就绪"
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$APP_PORT/health" >/dev/null 2>&1; then
    log "app 健康检查通过"
    break
  fi
  sleep 3
done
curl -fsS "http://127.0.0.1:$APP_PORT/health" >/dev/null || { warn "app 未就绪，查看：docker compose logs -f app"; exit 1; }

if [ -n "$DOMAIN" ] && [ "$DOMAIN" != "localhost" ]; then
  log "公网检查 https://$DOMAIN/health"
  for i in $(seq 1 20); do
    if curl -fsS "https://$DOMAIN/health" >/dev/null 2>&1; then log "HTTPS 正常"; break; fi
    sleep 5
  done
fi

cat <<EOF

部署完成
  访问地址：${DOMAIN:+https://$DOMAIN}${DOMAIN:-http://127.0.0.1:$APP_PORT}
  配置文件：$ENV_FILE
  查看日志：docker compose -f deploy/docker-compose.prod.yml logs -f app
  更新版本：cd $DIR && git pull && docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env up -d --build

下一步
  1. 补 XIANGKOU_LLM_API_KEY / MODEL 到 $ENV_FILE，再执行一次本脚本或 up -d
  2. 导入数据（首次）：bash deploy/init-db.sh
EOF
