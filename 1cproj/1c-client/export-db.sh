#!/bin/bash
. "$(dirname "$0")/common.sh"

usage() {
    cat <<USAGE
Выгрузка информационной базы в файл .dt на ЭТОТ компьютер (каталог EXPORT_DIR из .env, сейчас: $EXPORT_DIR).

  ./export-db.sh [база] [пользователь] [пароль]
        конфигуратор в клиентском контейнере подключается к серверу 1С по сети и сохраняет .dt здесь
  ./export-db.sh --cf [база] [пользователь] [пароль]
        то же для конфигурации: выгрузка .cf (файл кладётся в backup-каталог $BACKUP_SUBDIR)
  ./export-db.sh --pull
        забрать по scp самые свежие .dt/.cf, уже сделанные на сервере (./backup.sh), из SERVER_BACKUP_SRC
        (если сервер на этом же компьютере, SERVER_BACKUP_SRC=/путь/к/1c-server/backups, без ssh)
  ./export-db.sh --remote [база] [пользователь]
        по ssh запустить ./backup.sh на сервере, затем забрать готовые файлы сюда (пароль спросит сервер)

Все выгрузки (.dt/.cf) сохраняются в backup-подпапку: EXPORT_DIR/backup (на сервере — BACKUP_DIR).
Перед выгрузкой завершите сеансы в базе. Для первого режима нужен полный клиент (setup-full-*.run,
компонент client_full); тонкий клиент выгрузить базу не умеет. Графическая сессия не обязательна (xvfb).
Для --pull и --remote нужны ssh-доступ к серверу и SERVER_BACKUP_SRC в .env.
USAGE
}

split_src() {
    [ -n "$SERVER_BACKUP_SRC" ] || die "задайте SERVER_BACKUP_SRC в .env: пользователь@сервер:/путь/к/1c-server/backups (или просто /путь, если сервер на этом же компьютере)"
    case "$SERVER_BACKUP_SRC" in
        /*)  # локальный каталог: сервер на этом же компьютере
            SRC_HOST=""
            SRC_DIR="${SERVER_BACKUP_SRC%/}" ;;
        *:*)
            SRC_HOST="${SERVER_BACKUP_SRC%%:*}"
            SRC_DIR="${SERVER_BACKUP_SRC#*:}"
            SRC_DIR="${SRC_DIR%/}"
            if echo "${SRC_HOST#*@}" | grep -Eq '^172\.(1[6-9]|2[0-9]|3[01])\.'; then
                echo "ВНИМАНИЕ: ${SRC_HOST#*@} похож на внутренний адрес docker-контейнера. Нужен адрес КОМПЬЮТЕРА, где запущен 1c-server" >&2
                echo "(узнать на нём: hostname -I). Если сервер на этом же компьютере, укажите просто путь: SERVER_BACKUP_SRC=/путь/к/1c-server/backups" >&2
            fi ;;
        *) die "SERVER_BACKUP_SRC: формат пользователь@сервер:/путь или локальный путь /путь" ;;
    esac
}

pull_latest() {
    split_src
    local newest
    mkdir -p "$BACKUP_DIR"
    if [ -z "$SRC_HOST" ]; then
        newest=$(ls -t "$SRC_DIR"/dt/*.dt "$SRC_DIR"/cf/*.cf "$SRC_DIR"/*.dt "$SRC_DIR"/*.cf 2>/dev/null | head -n1)
        [ -n "$newest" ] || die "в $SRC_DIR нет .dt/.cf файлов (сначала ./backup.sh в 1c-server)"
        kind="${newest##*.}"
        mkdir -p "$BACKUP_DIR/$kind"
        echo "Копирую $newest в $BACKUP_DIR/$kind"
        cp -p "$newest" "$BACKUP_DIR/$kind/" || die "копирование не удалось"
    else
        newest=$(ssh "$SRC_HOST" "ls -t '$SRC_DIR'/*.dt '$SRC_DIR'/*.cf 2>/dev/null | head -n1") \
            || die "ssh к ${SRC_HOST} не удался: проверьте адрес компьютера сервера (не контейнера), что на нём запущен sshd и порт 22 открыт, и работает ли вход: ssh ${SRC_HOST}"
        [ -n "$newest" ] || die "на сервере в $SRC_DIR нет .dt/.cf файлов (сначала ./backup.sh на сервере)"
        echo "Копирую $SRC_HOST:$newest в $BACKUP_DIR"
        scp -p "$SRC_HOST:$newest" "$BACKUP_DIR/" || die "scp не удался"
    fi
    ls -lh "$BACKUP_DIR/$kind/$(basename "$newest")"
    echo "Готово: $(cd "$BACKUP_DIR/$kind" && pwd)/$(basename "$newest")"
}

# Экранирование аргумента одинарными кавычками для удалённой оболочки (любой, не только bash)
shq() { local s=${1//\'/\'\\\'\'}; printf "'%s'" "$s"; }

remote_backup() {
    split_src
    local proj a args=""
    proj=$(dirname "$SRC_DIR")
    if [ -z "$SRC_HOST" ]; then
        echo "Запускаю ./backup.sh в $proj ..."
        ( cd "$proj" && ./backup.sh "$@" ) || die "backup.sh завершился с ошибкой"
    else
        for a in "$@"; do args+="$(shq "$a") "; done
        echo "Запускаю ./backup.sh на сервере ($SRC_HOST:$proj) ..."
        ssh -t "$SRC_HOST" "cd $(shq "$proj") && ./backup.sh $args" \
            || die "backup.sh на сервере завершился с ошибкой"
    fi
    pull_latest
}

case "$1" in
    -h|--help) usage; exit 0 ;;
    --pull) pull_latest; exit 0 ;;
    --remote) shift; remote_backup "$@"; exit 0 ;;
esac

KIND=dt
if [ "$1" = "--cf" ]; then KIND=cf; shift; fi

IB="${1:-${IB_NAME:-mybase}}"
IB_USER="$2"
IB_PASS="$3"

[ -n "$SERVER_IP" ] || die "не задан SERVER_IP в .env"

# Конфигуратор всегда запускается в контейнере через обёртку 1c-xvfb: свой Xvfb,
# графическая сессия на этой машине не нужна и DISPLAY не используется.

$DOCKER compose up -d onec-client >/dev/null || die "не удалось поднять onec-client"

$DOCKER exec onec-client test -x /opt/1cv8/current/1cv8 \
    || die "в образе нет 1cv8 (конфигуратора). Положите в dist/ setup-full-<версия>-x86_64.run и пересоберите: ./build.sh && ./client.sh recreate. Либо используйте --pull / --remote (выгрузка на сервере)"

$DOCKER exec onec-client test -x /usr/local/bin/1c-xvfb \
    || die "в образе нет /usr/local/bin/1c-xvfb: пересоберите образ (./build.sh) и пересоздайте контейнер (./client.sh recreate)"
MISSING=$($DOCKER exec onec-client sh -c 'ldd /opt/1cv8/current/1cv8 2>&1 | grep "not found"' || true)
if [ -n "$MISSING" ]; then
    echo "$MISSING" >&2
    die "конфигуратору не хватает библиотек (список выше). Добавьте их пакеты в apt-get install в Dockerfile клиента и пересоберите образ; временно: sudo docker exec -u root onec-client sh -c 'apt-get update && apt-get install -y <пакет>'"
fi

if [ -n "$IB_USER" ] && [ -z "$IB_PASS" ]; then
    read -r -s -p "Пароль пользователя $IB_USER: " IB_PASS; echo
fi
AUTH=()
[ -n "$IB_USER" ] && AUTH=(/N "$IB_USER" /P "$IB_PASS")

STAMP=$(date +%Y%m%d_%H%M%S)
OUT="${IB}_${STAMP}.${KIND}"
LOG="export_${STAMP}.log"

# выгрузки .dt/.cf кладём в backup-подпапку каталога EXPORT_DIR (она видна на веб со вложенностями)
BACKUP_SUBDIR="${EXPORT_DIR%/}/backup/$KIND"
REMOTE_BACKUP="/home/user1c/export/backup/$KIND"
mkdir -p "$BACKUP_SUBDIR"
$DOCKER exec onec-client mkdir -p "$REMOTE_BACKUP" 2>/dev/null || true

# /DumpIB — выгрузка базы в .dt, /DumpCF — выгрузка конфигурации в .cf
if [ "$KIND" = "cf" ]; then OP=/DumpCF; else OP=/DumpIB; fi

echo "Выгрузка базы '$IB' (${KIND}) с onec-server:1541 в $BACKUP_SUBDIR/$OUT ..."
RC=0
$DOCKER exec onec-client \
    /usr/local/bin/1c-xvfb /opt/1cv8/current/1cv8 DESIGNER /S "onec-server:1541\\${IB}" "${AUTH[@]}" \
    /DisableStartupDialogs /DisableStartupMessages \
    "$OP" "${REMOTE_BACKUP}/${OUT}" \
    /Out "${REMOTE_BACKUP}/${LOG}" || RC=$?

# чтобы файлами мог управлять пользователь хоста
$DOCKER exec onec-client chmod a+rw "${REMOTE_BACKUP}/${OUT}" "${REMOTE_BACKUP}/${LOG}" 2>/dev/null || true

echo "--- журнал конфигуратора ---"
cat "$BACKUP_SUBDIR/${LOG}" 2>/dev/null || echo "(журнал пуст)"
echo "----------------------------"

if [ $RC -eq 0 ] && [ -s "$BACKUP_SUBDIR/${OUT}" ]; then
    ls -lh "$BACKUP_SUBDIR/${OUT}"
    echo "Готово: $(cd "$BACKUP_SUBDIR" && pwd)/${OUT}"
else
    die "выгрузка не удалась (код $RC). Проверьте журнал выше: пользователь/пароль, активные сеансы, лицензия"
fi
