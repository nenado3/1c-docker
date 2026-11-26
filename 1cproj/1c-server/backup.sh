#!/bin/bash
. "$(dirname "$0")/common.sh"

usage() {
    cat <<USAGE
Резервная копия информационной базы в каталог BACKUP_DIR (.env, по умолчанию ./backups)

  ./backup.sh [база] [пользователь] [пароль]    выгрузка в .dt (по умолчанию)
  ./backup.sh --cf [база] [пользователь] [пароль]    выгрузка конфигурации в .cf
  ./backup.sh --pg [база]                       дамп PostgreSQL (pg_dump -Fc)

.dt и .cf всегда кладутся в backup-каталог: серверный — в BACKUP_DIR (1c-server/backups),
клиентский export-db.sh — в EXPORT_DIR/backup (1c-client/export/backup). Файлы с исходными
именами видны на веб-панели (вкладка «Выгрузки») со всеми вложенными папками.

Для .dt/.cf в образе сервера нужен полный клиент (компонент client_full из setup-full-*.run);
конфигуратор работает на виртуальном дисплее xvfb, графическая сессия не нужна.
Перед выгрузкой завершите сеансы пользователей в базе.
USAGE
}

case "$1" in
    -h|--help) usage; exit 0 ;;
    --pg)
        shift
        DB="${1:-$DB_NAME}"
        [[ "$DB" =~ ^[A-Za-z0-9_.-]{1,63}$ && "$DB" != "." && "$DB" != ".." ]] || die "недопустимое имя базы PostgreSQL"
        PG_DIR="${BACKUP_DIR%/}/${DB}/pg"; LOG_DIR="${BACKUP_DIR%/}/${DB}/logs"
        mkdir -p "$PG_DIR" "$LOG_DIR" || die "не удалось создать каталоги базы $DB"
        OUT="$PG_DIR/${DB}_$(date +%Y%m%d_%H%M%S).dump"
        LOG="$LOG_DIR/pg_dump_$(date +%Y%m%d_%H%M%S).log"
        echo "Дамп PostgreSQL базы '$DB' в $OUT ..." | tee -a "$LOG"
        if $DOCKER compose exec -T postgres bash -c 'runuser -u postgres -- /usr/local/pgbin/pg_dump -Fc -d "$1"' _ "$DB" > "$OUT" 2>>"$LOG" && [ -s "$OUT" ]; then
            ls -lh "$OUT"
            echo "Восстановление: pg_restore -d <пустая база> файл.dump (на PostgreSQL с mchar и fulleq)."
            exit 0
        fi
        rm -f "$OUT"; die "pg_dump не удался (имя базы верное? контейнер postgres запущен?)" ;;
esac

KIND=dt
if [ "$1" = "--cf" ]; then KIND=cf; shift; fi

IB="${1:-$IB_NAME}"
[[ "$IB" =~ ^[A-Za-z0-9_.-]{1,64}$ && "$IB" != "." && "$IB" != ".." ]] || die "недопустимое имя информационной базы"
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
BACKUP_KIND_DIR="${BACKUP_DIR%/}/${IB}/$KIND"
LOG_DIR="${BACKUP_DIR%/}/${IB}/logs"
mkdir -p "$BACKUP_KIND_DIR" "$LOG_DIR" || die "не удалось создать каталоги резервных копий или журналов"
FILE="${IB}_${STAMP}.${KIND}"
LOGF="backup_${STAMP}.log"

# /DumpIB — выгрузка базы в .dt, /DumpCF — выгрузка конфигурации в .cf; файл кладётся
# в смонтированный backup-каталог контейнера (/home/usr1cv8/backups = BACKUP_DIR на хосте)
if [ "$KIND" = "cf" ]; then OP=/DumpCF; else OP=/DumpIB; fi

echo "Выгрузка базы '$IB' (${KIND}) в ${BACKUP_KIND_DIR}/$FILE ..."
RC=0
$DOCKER compose exec -T onec-server bash -c '
IB="$1"; U="$2"; P="$3"; OUT="$4"; LOG="$5"; OP="$6"
AUTH=(); [ -n "$U" ] && AUTH=(/N "$U" /P "$P")
/usr/local/bin/1c-xvfb /opt/1cv8/current/1cv8 DESIGNER /S "onec-server:1541\\${IB}" "${AUTH[@]}" \
    /DisableStartupDialogs /DisableStartupMessages "$OP" "$OUT" /Out "$LOG"
' _ "$IB" "$IB_USER" "$IB_PASS" "/home/usr1cv8/backups/$KIND/$FILE" "/home/usr1cv8/backups/logs/$LOGF" "$OP" || RC=$?

echo "--- журнал конфигуратора ---"
cat "${LOG_DIR}/$LOGF" 2>/dev/null || echo "(журнал пуст)"
echo "----------------------------"

$DOCKER compose exec -T onec-server chmod a+rw "/home/usr1cv8/backups/$KIND/$FILE" "/home/usr1cv8/backups/logs/$LOGF" 2>/dev/null || true
if [ $RC -eq 0 ] && [ -s "${BACKUP_KIND_DIR}/$FILE" ]; then
    ls -lh "${BACKUP_KIND_DIR}/$FILE"
    echo "Готово: $(cd "$BACKUP_KIND_DIR" && pwd)/$FILE"
else
    $DOCKER compose exec -T onec-server cat /tmp/xvfb-1c.log 2>/dev/null || true
    die "выгрузка не удалась (код $RC). Проверьте журнал выше: пользователь/пароль, активные сеансы, лицензия"
fi
