#!/bin/bash
cd "$(dirname "$0")"

# Если пользователь в группе docker: DOCKER=docker ./start.sh
DOCKER="${DOCKER:-sudo docker}"

# Версия и пароль БД берутся из .env
set -a; . ./.env; set +a

IB_NAME="${IB_NAME:-mybase}"
DB_NAME="${DB_NAME:-mybase}"
DB_USER="${DB_USER:-${POSTGRES_USER:-postgres}}"
# тот же пароль, что и POSTGRES_PASSWORD в docker-compose.yml/.env
DB_PASS="${DB_PASS:-${POSTGRES_PASSWORD:-change_me}}"
# /opt/1cv8/current создаётся при сборке образа (не зависит от архитектуры)
ONEC_BIN="/opt/1cv8/current"

die() { echo "ОШИБКА: $*" >&2; exit 1; }

# Каталоги, монтируемые в контейнер (создаём заранее, иначе Docker создаст их от root)
BACKUP_DIR="${BACKUP_DIR:-./backups}"
mkdir -p licenses "$BACKUP_DIR" "$BACKUP_DIR/dt" "$BACKUP_DIR/cf" && chmod 1777 licenses && chmod 777 "$BACKUP_DIR" "$BACKUP_DIR/dt" "$BACKUP_DIR/cf" 2>/dev/null || true
