#!/bin/bash
set -Eeuo pipefail
. "$(dirname "$0")/common.sh"

DB="${1:-}"
FILE="${2:-}"
CLEAN="${3:-no}"

[[ "$DB" =~ ^[A-Za-z0-9_.-]{1,63}$ ]] || die "недопустимое имя целевой базы PostgreSQL"
[[ -f "$FILE" && -s "$FILE" ]] || die "файл .dump не найден или пуст: $FILE"
[[ "$CLEAN" == "yes" || "$CLEAN" == "no" ]] || die "недопустимый режим очистки"

$DOCKER compose ps --status running --services | grep -qx postgres || die "контейнер PostgreSQL не запущен"
echo "Проверяю формат дампа PostgreSQL..."
if ! $DOCKER compose exec -T postgres bash -c 'runuser -u postgres -- /usr/local/pgbin/pg_restore --list' < "$FILE" >/dev/null; then
    die "файл не распознан как архив pg_dump (custom .dump, -Fc)"
fi

ARGS=(--exit-on-error --no-owner --no-privileges)
if [[ "$CLEAN" == "yes" ]]; then
    ARGS+=(--clean --if-exists)
    echo "Включена очистка существующих объектов базы '$DB'."
fi
LOG_DIR="$BACKUP_DIR/$DB/logs"
mkdir -p "$BACKUP_DIR/$DB/pg" "$LOG_DIR"
LOG="$LOG_DIR/pg_restore_$(date +%Y%m%d_%H%M%S).log"
echo "Восстанавливаю PostgreSQL dump в базу '$DB'..." | tee -a "$LOG"
if $DOCKER compose exec -T postgres bash -c 'DB="$1"; shift; runuser -u postgres -- /usr/local/pgbin/pg_restore "$@" -d "$DB"' _ "$DB" "${ARGS[@]}" < "$FILE" 2>&1 | tee -a "$LOG"; then
    echo "Восстановление PostgreSQL базы '$DB' завершено успешно." | tee -a "$LOG"
else
    die "pg_restore завершился с ошибкой. Журнал: $LOG. База могла быть восстановлена частично."
fi
