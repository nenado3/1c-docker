#!/bin/bash
. "$(dirname "$0")/common.sh"

usage() {
    cat <<USAGE
Использование:
  ./license.sh status     показать файлы лицензий в ./licenses
  ./license.sh add FILE   положить готовый .lic-файл и перезапустить сервер
Получение лицензии выполняется клиентом (папка 1c-client, ./license.sh activate).
USAGE
}

mkdir -p licenses && chmod 1777 licenses

case "$1" in
  status) ls -la licenses ;;
  activate)
    [ -n "$2" ] || die "укажите имя файла .lic"
    [[ "$2" =~ ^[A-Za-z0-9_.-]{1,176}\\.lic$ ]] || die "недопустимое имя файла лицензии"
    FILE="licenses/$2"
    [ -s "$FILE" ] || die "файл лицензии не найден или пуст: $FILE"
    echo "Файл лицензии: $FILE"
    echo "Перезапускаю контейнер onec-server, чтобы сервер 1С перечитал лицензию…"
    $DOCKER compose restart onec-server || die "не удалось перезапустить onec-server"
    echo "Перезапуск завершён. Сервер 1С должен перечитать лицензию $2."
    ;;
  add)
    [ -f "$2" ] || die "укажите существующий файл: ./license.sh add файл.lic"
    cp "$2" licenses/ && chmod 666 "licenses/$(basename "$2")"
    $DOCKER compose restart onec-server
    ;;
  *) usage ;;
esac
