#!/usr/bin/env bash
# Запуск инфраструктуры и создание новой информационной базы из .dt.
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "\${BASH_SOURCE[0]}")" && pwd -P)"
cd "$SCRIPT_DIR"
die(){ echo "ОШИБКА: $*" >&2; exit 1; }
[[ $# -eq 3 ]] || die "использование: start-with-dt.sh <имя_ИБ> <файл.dt> <абсолютный_путь>"
IB="$1"; FILE="$2"; UPLOAD_PATH="$3"
[[ "$IB" =~ ^[A-Za-z0-9_.-]{1,64}$ ]] || die "недопустимое имя ИБ"
[[ "$FILE" =~ ^[A-Za-z0-9_.-]{1,180}\.dt$ ]] || die "разрешён только файл .dt"
[[ "$UPLOAD_PATH" = "$SCRIPT_DIR"/backups/dt/* ]] || die "файл должен находиться в backups/dt"
[[ -s "$UPLOAD_PATH" ]] || die "файл .dt не найден или пуст: $UPLOAD_PATH"
echo "== Шаг 1/2. Запуск PostgreSQL и сервера 1С"
START_WITHOUT_BASE=1 bash "$SCRIPT_DIR/start.sh"
echo
echo "== Шаг 2/2. Создание новой базы и восстановление .dt"
bash "$SCRIPT_DIR/import.sh" "$IB" "$FILE" dt "" "" "" "" "$UPLOAD_PATH" new
echo
echo "Сервер запущен, база '$IB' восстановлена из '$FILE'."
