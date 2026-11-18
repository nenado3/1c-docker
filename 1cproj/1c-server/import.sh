#!/usr/bin/env bash
# Import .dt/.cf directly into PostgreSQL using ibcmd; register/update the IB with RAC.
set -Eeuo pipefail
umask 0027
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$SCRIPT_DIR"
. "$SCRIPT_DIR/common.sh"
die(){ echo "ОШИБКА: $*" >&2; exit 1; }
[[ $# -ge 3 ]] || die "использование: import.sh <ИБ> <файл> <dt|cf> [пользователь1С] [пароль1С] [пользовательБД] [парольБД] [абсолютный путь]"
IB="$1"; FILE="$2"; KIND="${3,,}"; USER_1C="${4:-}"; PASS_1C="${5:-}"
DB_USER_ARG="${6:-}"; DB_PASS_ARG="${7:-}"; UPLOAD_PATH="${8:-}"; NEW_BASE_ONLY="${9:-}"; DB_NAME_ARG="${10:-}"
[[ "$IB" =~ ^[A-Za-z0-9_.-]{1,64}$ ]] || die "недопустимое имя ИБ"
[[ "$FILE" =~ ^[A-Za-z0-9_.-]{1,180}\.(dt|cf)$ && "${FILE##*.}" == "$KIND" ]] || die "недопустимое имя/расширение файла"
[[ "$KIND" == dt || "$KIND" == cf ]] || die "тип должен быть dt или cf"
# Для новой ИБ нельзя подхватывать DB_NAME из .env: там может оставаться имя
# старой базы (например mybase), из-за чего новая ИБ mybase2 подключится к ней.
if [[ "$NEW_BASE_ONLY" == "new" ]]; then
  DB="${DB_NAME_ARG:-$IB}"
else
  DB="${DB_NAME_ARG:-${DB_NAME:-$IB}}"
fi
DB_USER="${DB_USER_ARG:-${DB_USER:-${POSTGRES_USER:-postgres}}}"
DB_PASS="${DB_PASS_ARG:-${DB_PASS:-${POSTGRES_PASSWORD:-}}}"
[[ "$DB" =~ ^[A-Za-z0-9_.-]{1,63}$ ]] || die "недопустимое имя БД PostgreSQL"
[[ "$DB_USER" =~ ^[A-Za-z0-9_.-]{1,64}$ && -n "$DB_PASS" ]] || die "не заданы корректные реквизиты PostgreSQL в .env"
BACKUP_ROOT="$(realpath -m -- "$BACKUP_DIR")"
if [[ -n "$UPLOAD_PATH" ]]; then HOST_FILE="$UPLOAD_PATH"; else HOST_FILE="$BACKUP_ROOT/$KIND/$FILE"; [[ -s "$HOST_FILE" ]] || HOST_FILE="$BACKUP_ROOT/$FILE"; fi
[[ -s "$HOST_FILE" ]] || die "файл не найден или пуст: $HOST_FILE"
HOST_FILE="$(realpath -- "$HOST_FILE")"
case "$HOST_FILE" in "$BACKUP_ROOT"/*) REL="${HOST_FILE#"$BACKUP_ROOT"/}";; *) die "файл должен находиться внутри BACKUP_DIR=$BACKUP_ROOT";; esac
CONTAINER_FILE="/home/usr1cv8/backups/$REL"
SIZE="$(stat -c %s "$HOST_FILE")"
LOG_DIR="$BACKUP_ROOT/import-logs"; mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d_%H%M%S)_$$"; LOG="$LOG_DIR/import_$STAMP.log"
: > "$LOG"; chmod 600 "$LOG"
log(){ echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
compose(){ $DOCKER compose "$@"; }
log "Источник: $HOST_FILE ($(numfmt --to=iec "$SIZE")); контейнерный путь: $CONTAINER_FILE"
log "ИБ=$IB; PostgreSQL=$DB на postgres:5432"
exec 9>"/tmp/1c-import-$IB.lock"
flock -n 9 || die "для ИБ '$IB' уже выполняется импорт"
compose ps --status running --services | grep -qx postgres || die "postgres не запущен"
compose ps --status running --services | grep -qx onec-server || die "onec-server не запущен"
compose exec -T onec-server test -r "$CONTAINER_FILE" || die "onec-server не видит файл. Проверьте mount BACKUP_DIR"
CSIZE="$(compose exec -T onec-server stat -c %s "$CONTAINER_FILE" | tr -d '\r')"
[[ "$CSIZE" == "$SIZE" ]] || die "размер файла в контейнере $CSIZE, ожидался $SIZE"
RAC=/opt/1cv8/current/rac; IBCMD=/opt/1cv8/current/ibcmd
compose exec -T onec-server test -x "$RAC" || die "rac отсутствует в образе"
compose exec -T onec-server test -x "$IBCMD" || die "ibcmd отсутствует в образе; нужна пересборка"
CLUSTER="$(compose exec -T onec-server "$RAC" cluster list | awk '/^cluster[[:space:]]*:/ {print $3; exit}' | tr -d '\r')"
[[ -n "$CLUSTER" ]] || die "RAC не вернул UUID кластера"
find_ib(){ awk -v wanted="$IB" '
function trim(s){sub(/^[ \t]+/,"",s);sub(/[ \t]+$/,"",s);gsub(/^"|"$/,"",s);return s}
 /^infobase[ \t]*:/ {id=$3}
 /^name[ \t]*:/ {n=trim(substr($0,index($0,":")+1));if(n==wanted){print id;exit}}
'; }
SUMMARY="$(compose exec -T onec-server "$RAC" infobase summary list --cluster="$CLUSTER")"
IBID="$(printf '%s\n' "$SUMMARY" | find_ib | tr -d '\r')"
if [[ "$NEW_BASE_ONLY" == "new" ]]; then
  [[ -z "$IBID" ]] || die "информационная база '$IB' уже существует в кластере; выберите другое имя"
  PG_EXISTS="$(compose exec -T postgres runuser -u postgres -- /usr/local/pgbin/psql -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname='$DB'")"
  [[ -z "$PG_EXISTS" ]] || die "база PostgreSQL '$DB' уже существует; выберите другое имя"
  log "Создаю новую регистрацию 1С и PostgreSQL-базу '$DB' для восстановления архива .dt."
fi
if [[ -n "$IBID" && "$NEW_BASE_ONLY" != "new" ]]; then
  [[ -n "$USER_1C" && -n "$PASS_1C" ]] || die "для изменения параметров существующей ИБ '$IB' укажите пользователя и пароль администратора 1С в веб-панели"
  log "ИБ уже зарегистрирована (UUID $IBID). Обновляю параметры СУБД с авторизацией администратора ИБ."
  compose exec -T onec-server "$RAC" infobase update --cluster="$CLUSTER" --infobase="$IBID" --infobase-user="$USER_1C" --infobase-pwd="$PASS_1C" --dbms=PostgreSQL --db-server="postgres port=5432" --db-name="$DB" --db-user="$DB_USER" --db-pwd="$DB_PASS" 2>&1 | tee -a "$LOG"
else
  log "Создаю регистрацию ИБ и PostgreSQL-базу '$DB'."
  compose exec -T onec-server "$RAC" infobase create --cluster="$CLUSTER" --name="$IB" --dbms=PostgreSQL --db-server="postgres port=5432" --db-name="$DB" --db-user="$DB_USER" --db-pwd="$DB_PASS" --locale=ru --create-database 2>&1 | tee -a "$LOG"
  if [[ "$NEW_BASE_ONLY" == "new" ]]; then
    PG_CREATED="$(compose exec -T postgres runuser -u postgres -- /usr/local/pgbin/psql -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname='$DB'")"
    [[ "$PG_CREATED" == "1" ]] || die "RAC зарегистрировал ИБ, но PostgreSQL-база '$DB' не найдена после команды --create-database"
    log "Проверка PostgreSQL: отдельная база '$DB' действительно создана."
  fi
  SUMMARY="$(compose exec -T onec-server "$RAC" infobase summary list --cluster="$CLUSTER")"
  IBID="$(printf '%s\n' "$SUMMARY" | find_ib | tr -d '\r')"
  [[ -n "$IBID" ]] || die "RAC не вернул UUID созданной ИБ"
fi
log "UUID ИБ: $IBID"
log "Перезапускаю onec-server для сброса кэша кластера. Текущие сеансы сервера будут отключены."
compose restart onec-server 2>&1 | tee -a "$LOG"
READY=0
for _ in $(seq 1 90); do
  if compose exec -T onec-server "$RAC" cluster list >/dev/null 2>&1; then READY=1; break; fi
  sleep 2
done
[[ "$READY" == 1 ]] || die "RAS не поднялся после перезапуска"
AUTH=()
if [[ -n "$USER_1C" ]]; then AUTH=(--user="$USER_1C" --password="$PASS_1C"); fi
if [[ "$KIND" == dt ]]; then
  log "ВНИМАНИЕ: восстановление .dt полностью заменит содержимое ИБ '$IB'. Запускаю ibcmd restore --force."
  compose exec -T onec-server "$IBCMD" infobase --dbms=PostgreSQL --db-server="postgres port=5432" --db-name="$DB" --db-user="$DB_USER" --db-pwd="$DB_PASS" restore --force "${AUTH[@]}" "$CONTAINER_FILE" 2>&1 | tee -a "$LOG"
else
  log "Загружаю .cf через ibcmd infobase config load."
  compose exec -T onec-server "$IBCMD" infobase --dbms=PostgreSQL --db-server="postgres port=5432" --db-name="$DB" --db-user="$DB_USER" --db-pwd="$DB_PASS" config load "${AUTH[@]}" "$CONTAINER_FILE" 2>&1 | tee -a "$LOG"
  log "Применяю конфигурацию через ibcmd infobase config apply."
  compose exec -T onec-server "$IBCMD" infobase --dbms=PostgreSQL --db-server="postgres port=5432" --db-name="$DB" --db-user="$DB_USER" --db-pwd="$DB_PASS" config apply "${AUTH[@]}" --force 2>&1 | tee -a "$LOG"
fi
log "Операция ibcmd завершилась успешно."
