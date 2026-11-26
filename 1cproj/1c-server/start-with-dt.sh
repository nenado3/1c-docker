#!/usr/bin/env bash
# Запуск инфраструктуры и создание новой информационной базы из .dt.
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
cd "$SCRIPT_DIR"
. "$SCRIPT_DIR/common.sh"
BACKUP_ROOT="$(realpath -m -- "$BACKUP_DIR")"
die(){ echo "ОШИБКА: $*" >&2; exit 1; }
[[ $# -ge 3 && $# -le 6 ]] || die "использование: start-with-dt.sh <имя_ИБ> <файл.dt> <абсолютный_путь> [имя_БД] [пользователь_БД] [пароль_БД]"
IB="$1"; FILE="$2"; UPLOAD_PATH="$3"; DB_NAME_ARG="${4:-$IB}"; DB_USER_ARG="${5:-}"; DB_PASS_ARG="${6:-}"
[[ "$IB" =~ ^[A-Za-z0-9_.-]{1,64}$ && "$IB" != "." && "$IB" != ".." ]] || die "недопустимое имя ИБ"
[[ "$FILE" =~ ^[A-Za-z0-9_.-]{1,180}\.dt$ ]] || die "разрешён только файл .dt"
case "$(realpath -m -- "$UPLOAD_PATH")" in "$BACKUP_ROOT/$IB"/dt/*) ;; *) die "файл должен находиться в BACKUP_DIR/$IB/dt";; esac
mkdir -p "$BACKUP_ROOT/$IB"/{dt,cf,pg,logs}
if [[ "$DB_NAME_ARG" != "$IB" ]]; then mkdir -p "$BACKUP_ROOT/$DB_NAME_ARG"/{dt,cf,pg,logs}; fi
[[ -s "$UPLOAD_PATH" ]] || die "файл .dt не найден или пуст: $UPLOAD_PATH"
echo "== Шаг 1/2. Запуск PostgreSQL и сервера 1С"
START_WITHOUT_BASE=1 bash "$SCRIPT_DIR/start.sh"
echo
echo "== Шаг 2/2. Создание новой базы и восстановление .dt"
bash "$SCRIPT_DIR/import.sh" "$IB" "$FILE" dt "" "" "$DB_USER_ARG" "$DB_PASS_ARG" "$UPLOAD_PATH" new "$DB_NAME_ARG"
echo
echo "Сервер запущен, база '$IB' восстановлена из '$FILE'."
