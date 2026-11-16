#!/bin/bash
. "$(dirname "$0")/common.sh"

usage() {
    cat <<USAGE
Резервная копия информационной базы в каталог BACKUP_DIR (.env, по умолчанию ./backups)

  ./backup.sh [база] [пользователь] [пароль]    выгрузка в .dt (по умолчанию)
  ./backup.sh --pg [база]                       дамп PostgreSQL (pg_dump -Fc)

Для .dt в образе сервера нужен полный клиент (компонент client_full из setup-full-*.run);
конфигуратор работает на виртуальном дисплее xvfb, графическая сессия не нужна.
Перед выгрузкой завершите сеансы пользователей в базе.
USAGE
}

case "$1" in
    -h|--help) usage; exit 0 ;;
    --pg)
        shift
        DB="${1:-$DB_NAME}"
        OUT="${BACKUP_DIR}/${DB}_$(date +%Y%m%d_%H%M%S).dump"
        echo "Дамп PostgreSQL базы '$DB' в $OUT ..."
        if $DOCKER compose exec -T postgres bash -c 'runuser -u postgres -- /usr/local/pgbin/pg_dump -Fc -d "$1"' _ "$DB" > "$OUT" && [ -s "$OUT" ]; then
            ls -lh "$OUT"
            echo "Восстановление: pg_restore -d <пустая база> файл.dump (на PostgreSQL с mchar и fulleq)."
            exit 0
        fi
        rm -f "$OUT"; die "pg_dump не удался (имя базы верное? контейнер postgres запущен?)" ;;
esac

IB="${1:-$IB_NAME}"
IB_USER="$2"
IB_PASS="$3"

$DOCKER compose ps --status running --services | grep -qx onec-server \
    || die "контейнер onec-server не запущен: ./start.sh"

$DOCKER compose exec -T onec-server test -x /opt/1cv8/current/1cv8 \
    || die "в образе сервера нет 1cv8 (конфигуратора). Положите в server/dist установщик setup-full-<версия>-x86_64.run и пересоберите: ./build.sh и ./start.sh. Дамп PostgreSQL: ./backup.sh --pg"

# Проверка обёртки с Xvfb: без неё 1С падает с "Unable to initialize GTK+"
$DOCKER compose exec -T onec-server test -x /usr/local/bin/1c-xvfb \
    || die "в образе сервера нет /usr/local/bin/1c-xvfb: пересоберите образ (./build.sh) и перезапустите (./start.sh)"
MISSING=$($DOCKER compose exec -T onec-server sh -c 'ldd /opt/1cv8/current/1cv8 2>&1 | grep "not found"' || true)
if [ -n "$MISSING" ]; then
    echo "$MISSING" >&2
    die "конфигуратору не хватает библиотек (список выше). Добавьте их пакеты в Dockerfile сервера (блок с libgtk-3-0 и xvfb) и пересоберите образ"
fi

if [ -n "$IB_USER" ] && [ -z "$IB_PASS" ]; then
    read -r -s -p "Пароль пользователя $IB_USER: " IB_PASS; echo
fi

STAMP=$(date +%Y%m%d_%H%M%S)
FILE="${IB}_${STAMP}.dt"
LOGF="backup_${STAMP}.log"

echo "Выгрузка базы '$IB' в ${BACKUP_DIR}/$FILE ..."
RC=0
$DOCKER compose exec -T onec-server bash -c '
IB="$1"; U="$2"; P="$3"; OUT="$4"; LOG="$5"
AUTH=(); [ -n "$U" ] && AUTH=(/N "$U" /P "$P")
/usr/local/bin/1c-xvfb /opt/1cv8/current/1cv8 DESIGNER /S "onec-server:1541\\${IB}" "${AUTH[@]}" \
    /DisableStartupDialogs /DisableStartupMessages /DumpIB "$OUT" /Out "$LOG"
' _ "$IB" "$IB_USER" "$IB_PASS" "/home/usr1cv8/backups/$FILE" "/home/usr1cv8/backups/$LOGF" || RC=$?

echo "--- журнал конфигуратора ---"
cat "${BACKUP_DIR}/$LOGF" 2>/dev/null || echo "(журнал пуст)"
echo "----------------------------"

$DOCKER compose exec -T onec-server chmod a+rw "/home/usr1cv8/backups/$FILE" "/home/usr1cv8/backups/$LOGF" 2>/dev/null || true
if [ $RC -eq 0 ] && [ -s "${BACKUP_DIR}/$FILE" ]; then
    ls -lh "${BACKUP_DIR}/$FILE"
    echo "Готово: $(cd "$BACKUP_DIR" && pwd)/$FILE"
else
    $DOCKER compose exec -T onec-server cat /tmp/xvfb-1c.log 2>/dev/null || true
    die "выгрузка не удалась (код $RC). Проверьте журнал выше: пользователь/пароль, активные сеансы, лицензия"
fi
