#!/bin/bash
. "$(dirname "$0")/common.sh"

mkdir -p licenses && chmod 1777 licenses

[ -n "${POSTGRES_PASSWORD:-}" ] || die "задайте уникальный POSTGRES_PASSWORD в .env до запуска PostgreSQL"

echo "== Запуск postgres и сервера 1С"
$DOCKER compose up -d || die "не удалось запустить контейнеры"

RAC() { $DOCKER compose exec -T onec-server "${ONEC_BIN}/rac" "$@"; }

echo "== Ожидание кластера 1С (до 3 минут)"
CLUSTER_ID=""
for i in $(seq 1 90); do
    CLUSTER_ID=$(RAC cluster list 2>/dev/null | awk '/^cluster/ {print $3; exit}')
    [ -n "$CLUSTER_ID" ] && break
    sleep 2
done

if [ -z "$CLUSTER_ID" ]; then
    echo "Кластер не найден. Вывод rac и логи сервера:"
    RAC cluster list || true
    $DOCKER compose logs --tail=30 onec-server
    die "сервер 1С не отвечает"
fi
echo "Кластер: $CLUSTER_ID"

if [[ "${START_WITHOUT_BASE:-0}" == "1" ]]; then
    echo "== Пропускаю создание пустой базы: будет восстановлен выбранный .dt"
else
echo "== Проверка информационной базы '$IB_NAME'"
if RAC infobase summary list --cluster="$CLUSTER_ID" | grep -qE "^name +: +${IB_NAME}$"; then
    echo "База '$IB_NAME' уже есть, пропускаю создание"
else
    echo "Создаю базу '$IB_NAME'"
    RAC infobase create \
        --cluster="$CLUSTER_ID" \
        --name="$IB_NAME" \
        --dbms=PostgreSQL \
        --db-server="postgres port=5432" \
        --db-name="$DB_NAME" \
        --db-user="$DB_USER" \
        --db-pwd="$DB_PASS" \
        --locale=ru \
        --create-database \
        || die "не удалось создать базу (смотрите ошибку СУБД выше)"
fi
fi

if [[ "${START_WITHOUT_BASE:-0}" != "1" ]]; then
    [[ "$IB_NAME" =~ ^[A-Za-z0-9_.-]{1,64}$ && "$IB_NAME" != "." && "$IB_NAME" != ".." ]] || die "недопустимое имя информационной базы в .env"
    [[ "$DB_NAME" =~ ^[A-Za-z0-9_.-]{1,63}$ && "$DB_NAME" != "." && "$DB_NAME" != ".." ]] || die "недопустимое имя PostgreSQL-базы в .env"
    mkdir -p "$BACKUP_DIR/$IB_NAME/dt" "$BACKUP_DIR/$IB_NAME/cf" "$BACKUP_DIR/$IB_NAME/logs" "$BACKUP_DIR/$DB_NAME/pg"
fi

echo
echo "Сервер готов. Сеть для клиента: onec-net"
echo "В клиенте: кластер onec-server:1541, база $IB_NAME"
