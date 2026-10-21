#!/usr/bin/env bash
# 初始化业务库：schema + 种子数据（只在首次部署时执行）。
# MySQL 未对公网暴露，所以通过容器执行。
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE="docker compose -f $DIR/deploy/docker-compose.prod.yml --env-file $DIR/deploy/.env"

echo "==> 等待 MySQL 健康"
for i in $(seq 1 60); do
  if $COMPOSE ps mysql 2>/dev/null | grep -q healthy; then break; fi
  sleep 2
done

echo "==> 导入 schema"
$COMPOSE exec -T mysql sh -c 'mysql --default-character-set=utf8mb4 -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"' < "$DIR/dev/schema.sql"

echo "==> 导入种子数据"
$COMPOSE exec -T mysql sh -c 'mysql --default-character-set=utf8mb4 -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"' < "$DIR/dev/02-seed-data.sql"

echo "==> 校验"
$COMPOSE exec -T mysql sh -c 'mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -e "SELECT CONCAT(\"shops=\", COUNT(*)) FROM tb_shop; SELECT CONCAT(\"vouchers=\", COUNT(*)) FROM tb_voucher; SELECT CONCAT(\"blogs=\", COUNT(*)) FROM tb_blog;"'
