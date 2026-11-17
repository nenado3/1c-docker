#!/usr/bin/env bash
# Надёжный импорт .dt/.cf в информационную базу 1С.
# Запуск из 1c-server: ./import.sh <ИБ> <имя-файла> <dt|cf> [пользователь 1С] [пароль 1С] [пользователь PostgreSQL] [пароль PostgreSQL] [абсолютный путь к загруженному файлу]
#
# PostgreSQL не восстанавливает .dt напрямую. Оба типа файлов обрабатываются
# конфигуратором 1С внутри onec-server; PostgreSQL используется самой платформой.

set -Eeuo pipefail
umask 0027

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$SCRIPT_DIR"

# common.sh задаёт DOCKER и загружает .env.
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }

[[ $# -ge 3 ]] || die "использование: ./import.sh <база> <файл.dt|файл.cf> <dt|cf> [пользователь 1С] [пароль 1С] [пользователь PostgreSQL] [пароль PostgreSQL] [абсолютный путь]"
IB="$1"
FILE="$2"
KIND="${3,,}"
USER_1C="${4:-}"
PASS_1C="${5:-}"
DB_USER_ARG="${6:-}"
DB_PASS_ARG="${7:-}"
UPLOAD_PATH_ARG="${8:-}"

[[ "$IB" =~ ^[A-Za-z0-9_.-]{1,64}$ ]] || die "недопустимое имя информационной базы"
[[ "$FILE" =~ ^[A-Za-z0-9_.-]{1,180}\.(dt|cf)$ ]] || die "недопустимое имя файла"
[[ "$KIND" == dt || "$KIND" == cf ]] || die "тип файла должен быть dt или cf"
[[ "${FILE##*.}" == "$KIND" ]] || die "расширение файла не соответствует типу $KIND"
[[ -z "$DB_USER_ARG" || "$DB_USER_ARG" =~ ^[A-Za-z0-9_.-]{1,64}$ ]] || die "недопустимое имя пользователя PostgreSQL"
[[ ${#DB_PASS_ARG} -le 200 ]] || die "слишком длинный пароль PostgreSQL"

# Ищем исходный файл строго по переданному веб-панелью пути либо в backups/<тип>.
if [[ -n "$UPLOAD_PATH_ARG" ]]; then
    HOST_FILE="$UPLOAD_PATH_ARG"
else
    HOST_FILE="$SCRIPT_DIR/backups/$KIND/$FILE"
    [[ -s "$HOST_FILE" ]] || HOST_FILE="$SCRIPT_DIR/backups/$FILE"
fi
[[ -f "$HOST_FILE" && -s "$HOST_FILE" ]] || die "исходный файл не найден или пуст: $HOST_FILE"
HOST_FILE="$(realpath -- "$HOST_FILE")"
FILE_SIZE="$(stat -c '%s' -- "$HOST_FILE")"
log "Источник: $HOST_FILE ($(numfmt --to=iec "$FILE_SIZE"))"

# Один общий каталог bind mount для обеих служб.
IMPORT_HOST_DIR="$SCRIPT_DIR/import"
mkdir -p -- "$IMPORT_HOST_DIR"
[[ -d "$IMPORT_HOST_DIR" ]] || die "не удалось создать $IMPORT_HOST_DIR"
# Контейнерный пользователь 1С имеет UID/GID 1001. Не меняем права файлов
# резервной копии; даём группе/владельцу возможность создавать логи в каталоге.
if [[ ! -w "$IMPORT_HOST_DIR" ]]; then
    if command -v sudo >/dev/null 2>&1; then
        sudo chown 1001:1001 "$IMPORT_HOST_DIR" || true
        sudo chmod 0775 "$IMPORT_HOST_DIR" || true
    fi
fi
chmod 0775 "$IMPORT_HOST_DIR" 2>/dev/null || true

# Защита от параллельных импортов в одну и ту же ИБ. Блокировка хранится на хосте
# и освобождается автоматически даже при аварийном завершении shell-процесса.
LOCK_DIR="$IMPORT_HOST_DIR/.locks"
mkdir -p "$LOCK_DIR"
LOCK_FILE="$LOCK_DIR/$IB.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    die "для базы '$IB' уже выполняется импорт. Дождитесь его завершения; второй RestoreIB запускать нельзя"
fi

# Проверяем доступность Docker и контейнеров.
$DOCKER compose ps --status running --services | grep -qx postgres || die "контейнер postgres не запущен"
$DOCKER compose ps --status running --services | grep -qx onec-server || die "контейнер onec-server не запущен"

# Уникальные имена временной копии и журналов.
STAMP="$(date +%Y%m%d_%H%M%S)_$$"
IMPORT_FILE="${STAMP}_$FILE"
HOST_IMPORT_FILE="$IMPORT_HOST_DIR/$IMPORT_FILE"
REMOTE_FILE="/import/$IMPORT_FILE"
LOG="$IMPORT_HOST_DIR/import_${STAMP}.log"
REMOTE_LOG="/import/import_${STAMP}.log"
OUT_LOG="$IMPORT_HOST_DIR/configurator_${STAMP}.log"
REMOTE_OUT="/import/configurator_${STAMP}.log"
: > "$LOG"
chmod 0664 "$LOG"

cleanup() {
    local rc=$?
    if [[ -n "${HOST_IMPORT_FILE:-}" && -f "$HOST_IMPORT_FILE" ]]; then
        rm -f -- "$HOST_IMPORT_FILE"
    fi
    if [[ $rc -ne 0 ]]; then
        log "Импорт завершился с ошибкой (код $rc). Файл журнала сохранён: $LOG"
        log "Журнал конфигуратора (если создан): $OUT_LOG"
    else
        log "Импорт завершён успешно. Временный файл удалён; исходная резервная копия сохранена."
        rm -f -- "$LOG" "$OUT_LOG"
    fi
}
trap cleanup EXIT

# Сначала копируем во временный файл, затем атомарно публикуем его в bind mount.
PART_FILE="$HOST_IMPORT_FILE.part"
cp --reflink=auto -- "$HOST_FILE" "$PART_FILE"
chmod 0644 "$PART_FILE"
COPIED_SIZE="$(stat -c '%s' -- "$PART_FILE")"
[[ "$COPIED_SIZE" == "$FILE_SIZE" ]] || die "неполная копия файла ($COPIED_SIZE из $FILE_SIZE байт)"
mv -- "$PART_FILE" "$HOST_IMPORT_FILE"

for SERVICE in onec-server postgres; do
    $DOCKER compose exec -T "$SERVICE" test -r "$REMOTE_FILE" \
        || die "файл не читается внутри контейнера $SERVICE: $REMOTE_FILE"
    CONTAINER_SIZE="$($DOCKER compose exec -T "$SERVICE" stat -c '%s' "$REMOTE_FILE" | tr -d '\r')"
    [[ "$CONTAINER_SIZE" == "$FILE_SIZE" ]] || die "размер файла в $SERVICE не совпадает с оригиналом"
done

# Убедимся, что пользователь 1С может создавать /Out и диагностические журналы.
if ! $DOCKER compose exec -T --user 1001:1001 onec-server sh -c \
    'f="/import/.write-test-$$"; : > "$f" && rm -f "$f"'; then
    die "пользователь usr1cv8 не может записывать в /import. На хосте выполните: sudo chown 1001:1001 import && sudo chmod 775 import"
fi

# Имя PostgreSQL остаётся согласованным с конфигурацией проекта.
DB="$IB"
if [[ -n "${DB_NAME:-}" && "$IB" == "${IB_NAME:-$IB}" ]]; then
    DB="$DB_NAME"
fi
log "База 1С: $IB; база PostgreSQL: $DB"
log "Временный файл доступен в обоих контейнерах: $REMOTE_FILE"
log "Журнал выполнения: $LOG"

# Проверяем наличие бинарного файла и Xvfb-обёртки в контейнере.
$DOCKER compose exec -T onec-server test -x /opt/1cv8/current/1cv8 \
    || die "не найден конфигуратор /opt/1cv8/current/1cv8 в onec-server"
$DOCKER compose exec -T onec-server test -x /usr/local/bin/1c-xvfb \
    || die "не найдена /usr/local/bin/1c-xvfb в onec-server"

# Определяем реквизиты 1С. Для новой базы без пользователей оставляем их пустыми.
AUTH_ARGS=()
if [[ -n "$USER_1C" ]]; then
    AUTH_ARGS=(/N "$USER_1C" /P "$PASS_1C")
fi

# Один вызов DESIGNER на одну операцию. stdout/stderr docker exec одновременно
# показываются в веб-панели и сохраняются в файл на хосте. /Out — отдельный
# журнал, который создаёт сам конфигуратор.
DESIGNER_ARGS=(DESIGNER /S "onec-server:1541\\$IB")
if [[ "$KIND" == dt ]]; then
    DESIGNER_ARGS+=(/RestoreIB "$REMOTE_FILE")
    log "ВНИМАНИЕ: восстановление .dt заменяет данные информационной базы."
    log "Запускаю восстановление базы '$IB' из $FILE."
else
    DESIGNER_ARGS+=(/LoadCfg "$REMOTE_FILE" /UpdateDBCfg)
    log "Загружаю .cf и обновляю конфигурацию базы '$IB'."
fi
DESIGNER_ARGS+=("${AUTH_ARGS[@]}" /Out "$REMOTE_OUT")

run_designer() {
    local pid elapsed=0 rc=0
    (
        set -o pipefail
        $DOCKER compose exec -T onec-server \
            /usr/local/bin/1c-xvfb /opt/1cv8/current/1cv8 "${DESIGNER_ARGS[@]}" 2>&1 \
            | tee -a "$LOG"
    ) &
    pid=$!
    while kill -0 "$pid" 2>/dev/null; do
        sleep 10
        elapsed=$((elapsed + 10))
        if kill -0 "$pid" 2>/dev/null; then
            printf 'Конфигуратор выполняется: %02d:%02d\n' "$((elapsed / 60))" "$((elapsed % 60))"
            if (( elapsed % 30 == 0 )); then
                printf '%s\n' "--- диагностика: $((elapsed / 60)) мин ---"
                $DOCKER exec onec-server sh -c '
                    for d in /proc/[0-9]*; do
                        [ -r "$d/cmdline" ] || continue
                        cmd="$(tr "\000" " " < "$d/cmdline" 2>/dev/null || true)"
                        case "$cmd" in
                            *"/opt/1cv8/current/1cv8 DESIGNER"*"/RestoreIB"*|*"/opt/1cv8/current/1cv8 DESIGNER"*"/LoadCfg"*)
                                p="${d##*/}"
                                echo "PID $p"
                                grep -E "^(State|VmRSS|voluntary_ctxt_switches|nonvoluntary_ctxt_switches):" "$d/status" 2>/dev/null || true
                                grep -E "^(read_bytes|write_bytes):" "$d/io" 2>/dev/null || true
                                ;;
                        esac
                    done
                ' 2>&1 | tee -a "$LOG" || true
                $DOCKER exec onec-server sh -c \
                    "if [ -f '$REMOTE_OUT' ]; then echo '--- /Out ---'; tail -n 15 '$REMOTE_OUT'; else echo 'Файл /Out пока не создан'; fi" \
                    2>&1 | tee -a "$LOG" || true
                $DOCKER compose exec -T postgres runuser -u postgres -- /usr/local/pgbin/psql \
                    -d postgres -P pager=off -c \
                    "SELECT pid,datname,usename,state,wait_event_type,wait_event,now()-query_start AS query_time,left(query,120) AS query FROM pg_stat_activity WHERE backend_type='client backend' ORDER BY pid;" \
                    2>&1 | tee -a "$LOG" || true
            fi
        fi
    done
    wait "$pid" || rc=$?
    return "$rc"
}

if run_designer; then
    log "Конфигуратор завершился с кодом 0."
else
    rc=$?
    log "Конфигуратор завершился с кодом $rc."
    if $DOCKER exec onec-server test -f "$REMOTE_OUT"; then
        log "Последние строки журнала конфигуратора:"
        $DOCKER exec onec-server tail -n 80 "$REMOTE_OUT" 2>&1 | tee -a "$LOG" || true
    fi
    if $DOCKER exec onec-server test -f /tmp/xvfb-1c.log; then
        log "Журнал Xvfb:"
        $DOCKER exec onec-server tail -n 40 /tmp/xvfb-1c.log 2>&1 | tee -a "$LOG" || true
    fi
    die "операция $KIND не выполнена; исходный файл не удалён"
fi

# Успех: исходный .dt/.cf в backups/<тип> не трогаем. Удаляем только временную
# копию в /import и технические журналы, если команда действительно завершилась с rc=0.
exit 0
