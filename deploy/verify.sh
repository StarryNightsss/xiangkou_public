#!/usr/bin/env bash
# 部署后验收：容器、健康、核心接口、数据量、上传目录。
set -uo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$DIR/deploy/.env"
COMPOSE="docker compose -f $DIR/deploy/docker-compose.prod.yml --env-file $ENV_FILE"
PORT="$(grep -E '^APP_PORT=' "$ENV_FILE" 2>/dev/null | cut -d= -f2)"
PORT="${PORT:-8090}"
BASE="http://127.0.0.1:$PORT"
DOMAIN="$(grep -E '^DOMAIN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2)"
pass=0; fail=0

check() {
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "  PASS  $name"; pass=$((pass+1))
  else
    echo "  FAIL  $name"; fail=$((fail+1))
  fi
}

echo "== 容器 =="
for service in mysql redis go-api app; do
  check "$service running" bash -c "$COMPOSE ps $service | grep -q -E 'Up|running'"
done

echo "== 健康与接口 =="
check "app /health" curl -fsS "$BASE/health"
check "分类列表" bash -c "curl -fsS '$BASE/api/catalog/types' | grep -q '\"success\":true'"
check "店铺搜索" bash -c "curl -fsS '$BASE/api/catalog/shops?typeId=1&page=1&size=1' | grep -q '\"success\":true'"
check "go-api 直连" bash -c "$COMPOSE exec -T app python -c \"import urllib.request;urllib.request.urlopen('http://go-api:8081/health')\""

echo "== 数据 =="
counts=$($COMPOSE exec -T mysql sh -c 'mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -e "SELECT COUNT(*) FROM tb_shop; SELECT COUNT(*) FROM tb_voucher; SELECT COUNT(*) FROM tb_blog;"' 2>/dev/null | tr '\n' ' ')
echo "  shops/vouchers/blogs = $counts"
check "有店铺数据" bash -c "[ \"$(echo "$counts" | awk '{print $1}')\" -gt 0 ]"

echo "== 静态与上传 =="
check "前端首页" bash -c "curl -fsS '$BASE/' | grep -qi '<title>'"
check "静态资源" bash -c "curl -fsS '$BASE/' | grep -o '/web/assets/[^\"]*\.js' | head -1 | xargs -I{} curl -fsS '$BASE{}'"

if [ -n "${DOMAIN:-}" ] && [ "$DOMAIN" != "localhost" ]; then
  echo "== 公网 =="
  check "https://$DOMAIN/health" curl -fsS "https://$DOMAIN/health"
fi

echo
echo "通过 $pass 项，失败 $fail 项"
[ "$fail" -eq 0 ] || exit 1
