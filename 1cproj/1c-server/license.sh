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
  add)
    [ -f "$2" ] || die "укажите существующий файл: ./license.sh add файл.lic"
    cp "$2" licenses/ && chmod 666 "licenses/$(basename "$2")"
    $DOCKER compose restart onec-server
    ;;
  *) usage ;;
esac
