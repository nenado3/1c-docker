#!/bin/bash
# Импорт .dt/.cf в информационную базу кластера 1С.
#
# Признак «база пустая» — отсутствие пользователей 1С (представление v8users), а не отсутствие
# таблиц: при создании базы 1С сразу создаёт служебные таблицы. В пустой базе пользователей нет,
# поэтому ibcmd аутентифицироваться не может («Идентификация пользователя не выполнена»),
# и восстановление делает конфигуратор — он входит без логина.
# Если пользователи есть: сначала ibcmd (быстро, без графики), при отказе — конфигуратор с логином.
#
# Использование: ./import.sh <база> <файл> [dt|cf] [пользователь 1С] [пароль 1С] [пользователь PostgreSQL] [пароль PostgreSQL]
. "$(dirname "$0")/common.sh"
set -u
set -o pipefail

die() { echo "ОШИБКА: $*" >&2; exit 1; }
[ $# -ge 2 ] || die "использование: ./import.sh <база> <файл.dt|файл.cf> [dt|cf] [пользователь 1С] [пароль 1С] [пользователь PostgreSQL] [пароль PostgreSQL]"
IB="$1"
FILE="$2"
KIND="${3:-}"
USER_1C="${4:-}"
PASS_1C="${5:-}"
DB_USER_ARG="${6:-}"
DB_PASS_ARG="${7:-}"
[[ "$IB" =~ ^[A-Za-z0-9_.-]{1,64}$ ]] || die "недопустимое имя базы"
[[ "$FILE" =~ ^[A-Za-z0-9_.-]{1,180}\.(dt|cf)$ ]] || die "недопустимое имя файла"
[[ "$KIND" == "dt" || "$KIND" == "cf" ]] || die "тип файла должен быть dt или cf"

# .env читается из каталога проекта (cwd задаётся менеджером).
if [[ -f ./.env ]]; then
    set -a
    . ./.env
    set +a
fi
BACKUP_DIR="${BACKUP_DIR:-./backups}"
if [[ "$BACKUP_DIR" = /* ]]; then
    BACKUP_ROOT="$BACKUP_DIR"
else
    BACKUP_ROOT="$(pwd)/$BACKUP_DIR"
fi
HOST_FILE="$BACKUP_ROOT/$KIND/$FILE"
# Совместимость со старыми резервными копиями, которые лежали прямо в BACKUP_DIR.
if [[ ! -s "$HOST_FILE" && -s "$BACKUP_ROOT/$FILE" ]]; then
    HOST_FILE="$BACKUP_ROOT/$FILE"
fi

# Веб-панель и shell-скрипт могут запускаться с разными cwd/настройками BACKUP_DIR.
# После проверки стандартных мест ищем файл по точному имени в каталоге проекта
# и в настроенном BACKUP_DIR. Это устраняет ситуацию, когда загрузка прошла,
# но импорт пытается передать в docker cp устаревший/неверно вычисленный путь.
if [[ ! -s "$HOST_FILE" ]]; then
    PROJECT_BACKUPS="$(cd "$(dirname "$0")" && pwd)/backups"
    for SEARCH_ROOT in "$BACKUP_ROOT" "$PROJECT_BACKUPS"; do
        [[ -d "$SEARCH_ROOT" ]] || continue
        FOUND_FILE="$(find "$SEARCH_ROOT" -type f -name "$FILE" -print -quit 2>/dev/null || true)"
        if [[ -n "$FOUND_FILE" && -s "$FOUND_FILE" ]]; then
            HOST_FILE="$FOUND_FILE"
            echo "Файл найден в альтернативном каталоге резервных копий: $HOST_FILE"
            break
        fi
    done
fi

[[ -s "$HOST_FILE" ]] || {
    echo "ОШИБКА: файл не найден на сервере." >&2
    echo "Проверенные пути: $BACKUP_ROOT/$KIND/$FILE и $BACKUP_ROOT/$FILE" >&2
    echo "Каталог BACKUP_DIR: $BACKUP_ROOT" >&2
    echo "Содержимое каталога типа $KIND:" >&2
    ls -lah "$BACKUP_ROOT/$KIND" 2>&1 | tail -n 30 >&2 || true
    die "проверьте, что файл загрузился на сервер и BACKUP_DIR в веб-панели совпадает с 1c-server/.env"
}

# Имя базы PostgreSQL: у базы по умолчанию это DB_NAME из .env (её создаёт start.sh),
# у остальных баз (созданы панелью) — совпадает с именем информационной базы.
DB="$IB"
if [ -n "${DB_NAME:-}" ] && [ "$IB" = "${IB_NAME:-$IB}" ]; then DB="$DB_NAME"; fi
[[ "$DB" =~ ^[A-Za-z0-9_.-]{1,64}$ ]] || die "недопустимое имя базы PostgreSQL: $DB"

# Пользователь/пароль PostgreSQL можно передать из веб-панели (поля «Пользователь PostgreSQL»).
# Если пользователь не задан — берутся DB_USER/DB_PASS из .env (common.sh).
if [ -n "$DB_USER_ARG" ]; then
    [[ "$DB_USER_ARG" =~ ^[A-Za-z0-9_.-]{1,64}$ ]] || die "недопустимое имя пользователя PostgreSQL"
    DB_USER="$DB_USER_ARG"
    DB_PASS="${DB_PASS_ARG:-$DB_PASS}"
fi

$DOCKER compose ps --status running --services | grep -qx onec-server || die "контейнер onec-server не запущен: ./start.sh"
PGBIN=/usr/local/pgbin

# Пользователи 1С: представление v8users создаёт платформа. Нет представления/строк — база пустая.
USERS=$($DOCKER compose exec -T postgres runuser -u postgres -- "$PGBIN/psql" -d "$DB" -tAc \
    "SELECT count(*) FROM v8users" 2>/dev/null | tr -d '[:space:]')
case "$USERS" in ''|*[!0-9]*) USERS=0;; esac

# Передаём файл в контейнер onec-server через docker cp.
# Основной путь — смонтированный backup-каталог /home/usr1cv8/backups (файл с исходным именем
# остаётся виден в BACKUP_DIR на хосте и в веб-панели); /tmp используется как запасной вариант,
# если bind-mount BACKUP_DIR в контейнере нет (веб-панель и compose могут иметь разные cwd).
[ -r "$HOST_FILE" ] || die "файл на хосте недоступен для чтения: $HOST_FILE"
FILE_SIZE=$(stat -c '%s' "$HOST_FILE")
[ "$FILE_SIZE" -gt 0 ] || die "файл на хосте пустой: $HOST_FILE"

REMOTE_DIR="/home/usr1cv8/backups/$KIND"
CONTAINER="onec-server"

# Проверяем контейнер до работы с каталогами.
$DOCKER inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -qx true \
    || die "контейнер $CONTAINER не запущен; сначала запустите сервер 1С"

if ! $DOCKER exec "$CONTAINER" test -d "$REMOTE_DIR" 2>/dev/null; then
    # если смонтированного backup-каталога в контейнере нет — используем /tmp
    REMOTE_DIR="/tmp/onec-import"
fi
REMOTE="$REMOTE_DIR/$FILE"
$DOCKER exec "$CONTAINER" mkdir -p "$REMOTE_DIR" \
    || die "не удалось создать каталог $REMOTE_DIR в контейнере"

# Сначала удаляем одноимённый остаток предыдущего прерванного импорта.
$DOCKER exec "$CONTAINER" rm -f "$REMOTE" 2>/dev/null || true
echo "Передача файла в контейнер $CONTAINER (размер: $(numfmt --to=iec "$FILE_SIZE"))..."
$DOCKER cp "$HOST_FILE" "$CONTAINER:$REMOTE" \
    || die "docker cp не смог передать файл в контейнер $CONTAINER"

# Проверяем не только наличие, но и точное совпадение размера.
REMOTE_SIZE=$($DOCKER exec "$CONTAINER" stat -c '%s' "$REMOTE" 2>/dev/null) \
    || die "файл скопирован, но не найден внутри контейнера: $REMOTE"
[[ "$REMOTE_SIZE" =~ ^[0-9]+$ ]] || die "не удалось определить размер файла в контейнере: $REMOTE"
[ "$REMOTE_SIZE" -eq "$FILE_SIZE" ] \
    || die "файл передан не полностью: на хосте $FILE_SIZE байт, в контейнере $REMOTE_SIZE байт"
$DOCKER exec "$CONTAINER" test -r "$REMOTE" \
    || die "файл существует, но недоступен для чтения в контейнере: $REMOTE"
echo "Файл успешно передан и проверен: $REMOTE ($REMOTE_SIZE байт)"

STAMP=$(date +%Y%m%d_%H%M%S)
LOG="$(dirname "$REMOTE")/import_${STAMP}.log"
ACTION=()
CAUTH=()
[[ -n "$USER_1C" ]] && CAUTH=(/N "$USER_1C" /P "$PASS_1C")

# Запуск конфигуратора в контейнере: свой Xvfb, графическая сессия не нужна.
designer_run() {
    $DOCKER compose exec -T onec-server \
        /usr/local/bin/1c-xvfb /opt/1cv8/current/1cv8 DESIGNER \
        /S "onec-server:1541\\${IB}" "$@" "${ACTION[@]}" /Out "$LOG"
}

# ibcmd не выдаёт надёжный процент восстановления. Показываем время выполнения,
# не подменяя его фиктивным процентом; вывод самой команды остаётся в журнале задачи.
run_with_progress() {
    local pid rc=0 secs=0
    "$@" &
    pid=$!
    while kill -0 "$pid" 2>/dev/null; do
        printf 'Операция 1С выполняется... %02d:%02d\n' "$((secs/60))" "$((secs%60))"
        sleep 10
        secs=$((secs+10))
    done
    wait "$pid" || rc=$?
    return "$rc"
}

designer_try() {
    $DOCKER compose exec -T onec-server test -x /opt/1cv8/current/1cv8 \
        || die "в образе нет конфигуратора 1С (компонент client_full): пересоберите образ"
    $DOCKER compose exec -T onec-server test -x /usr/local/bin/1c-xvfb \
        || die "в образе нет /usr/local/bin/1c-xvfb: пересоберите образ сервера (./build.sh)"
    local rc=0 pid secs=0
    # конфигуратор не сообщает процент выполнения — показываем ход по времени
    designer_run "$@" &
    pid=$!
    while kill -0 "$pid" 2>/dev/null; do
        printf 'Операция 1С выполняется... %02d:%02d\n' "$((secs/60))" "$((secs%60))"
        sleep 10; secs=$((secs+10))
    done
    wait "$pid" || rc=$?
    echo "--- журнал конфигуратора ---"
    $DOCKER compose exec -T onec-server cat "$LOG" 2>/dev/null || true
    echo "----------------------------"
    if [ $rc -ne 0 ]; then
        $DOCKER compose exec -T onec-server cat /tmp/xvfb-1c.log 2>/dev/null || true
    fi
    return $rc
}

# Восстановление/загрузка конфигуратором с учётом наличия пользователей 1С.
designer_restore() {
    if [ "$USERS" -gt 0 ]; then
        designer_try "${CAUTH[@]}"
        return $?
    fi
    designer_try && return 0
    if [ ${#CAUTH[@]} -gt 0 ]; then
        echo "Повторяю с пользователем 1С '$USER_1C' ..."
        designer_try "${CAUTH[@]}"
        return $?
    fi
    return 1
}

ibcmd_dt() {
    $DOCKER compose exec -T onec-server /opt/1cv8/current/ibcmd infobase \
        --dbms=PostgreSQL "--db-server=postgres port=5432" "--db-name=$DB" \
        "--db-user=$DB_USER" "--db-pwd=$DB_PASS" \
        restore --force --session-terminate-message="Идёт загрузка базы" \
        --user="$USER_1C" --password="$PASS_1C" "$REMOTE"
}

ibcmd_cf() {
    local base=(/opt/1cv8/current/ibcmd infobase)
    local db=(--dbms=PostgreSQL "--db-server=postgres port=5432" "--db-name=$DB"
              "--db-user=$DB_USER" "--db-pwd=$DB_PASS")
    $DOCKER compose exec -T onec-server "${base[@]}" config load "${db[@]}" \
        --user="$USER_1C" --password="$PASS_1C" "$REMOTE" \
        && $DOCKER compose exec -T onec-server "${base[@]}" config apply "${db[@]}" \
            --user="$USER_1C" --password="$PASS_1C" --force --session-terminate=force
}

# ---------------- .dt ----------------
if [[ "$KIND" == "dt" ]]; then
    ACTION=(/RestoreIB "$REMOTE")
    echo "Восстановление базы '$IB' (PostgreSQL: $DB, пользователь $DB_USER) из $FILE ..."
    echo "ВНИМАНИЕ: текущие данные базы будут заменены содержимым .dt."

    DONE=0
    if [ "$USERS" -gt 0 ] && [ -n "$USER_1C" ] && $DOCKER compose exec -T onec-server test -x /opt/1cv8/current/ibcmd; then
        echo "== Попытка через ibcmd (пользователь 1С: $USER_1C) ..."
        run_with_progress ibcmd_dt && DONE=1
    fi

    if [ "$DONE" != "1" ]; then
        if [ "$USERS" -eq 0 ]; then
            echo "В базе нет пользователей 1С (новая база) — восстанавливаю конфигуратором без логина."
        else
            [ -n "$USER_1C" ] || die "в базе '$DB' есть пользователи 1С: укажите логин и пароль 1С (та же учётная запись, что в базе)"
            echo "ibcmd не выполнил восстановление — повторяю конфигуратором."
        fi
        designer_restore || die "восстановление .dt не удалось"
    fi
    echo "Готово: база '$IB' восстановлена из '$FILE'."
# файл в backup-каталоге оставляем с исходным именем (он нужен в BACKUP_DIR и на веб-панели);
# удаляем только временный вариант из /tmp и журнал
case "$REMOTE" in /tmp/*) $DOCKER exec onec-server rm -f "$REMOTE" ;; esac
$DOCKER exec onec-server rm -f "$LOG" >/dev/null 2>&1 || true
exit 0
fi

# ---------------- .cf ----------------
ACTION=(/LoadCfg "$REMOTE" /UpdateDBCfg)
echo "Загрузка конфигурации из $FILE в базу '$IB' ..."
DONE=0
if [ "$USERS" -gt 0 ] && [ -n "$USER_1C" ] && $DOCKER compose exec -T onec-server test -x /opt/1cv8/current/ibcmd; then
    echo "== Попытка через ibcmd ..."
    run_with_progress ibcmd_cf && DONE=1
fi

if [ "$DONE" != "1" ]; then
    if [ "$USERS" -eq 0 ]; then
        echo "В базе нет пользователей 1С (новая база) — загружаю конфигуратором без логина."
    else
        [ -n "$USER_1C" ] || die "в базе '$DB' есть пользователи 1С: укажите логин и пароль 1С"
        echo "ibcmd конфигурацию не применил — загружаю конфигуратором."
    fi
    designer_restore || die "загрузка конфигурации не удалась"
fi
echo "Готово: конфигурация из '$FILE' загружена."
# файл в backup-каталоге оставляем с исходным именем; удаляем только временный вариант и журнал
case "$REMOTE" in /tmp/*) $DOCKER exec onec-server rm -f "$REMOTE" ;; esac
$DOCKER exec onec-server rm -f "$LOG" >/dev/null 2>&1 || true
