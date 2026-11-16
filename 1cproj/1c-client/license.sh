#!/bin/bash
. "$(dirname "$0")/common.sh"

usage() {
    cat <<USAGE
Использование:
  ./license.sh activate         открыть клиент для получения лицензии (окно активации)
  ./license.sh install          собрать полученные .lic в ./licenses
  ./license.sh push USER@HOST:/путь/к/1c-server/licenses/
                                отправить .lic на сервер по scp
  ./license.sh status           показать файлы лицензий
После push на сервере выполните: sudo docker restart onec-server
USAGE
}

mkdir -p licenses && chmod 1777 licenses

case "$1" in
  activate)
    echo "Откроется клиент 1С. Получите лицензию, закройте окно и выполните ./license.sh install"
    ./client.sh
    ;;
  install)
    $DOCKER compose up -d onec-client >/dev/null
    FILES=$($DOCKER exec onec-client bash -c "find /home/user1c -name '*.lic' 2>/dev/null")
    if [ -z "$FILES" ]; then
        echo "В домашнем каталоге клиента .lic не найдено."
        exit 1
    fi
    echo "$FILES" | while read -r f; do
        echo "Копирую $f"
        $DOCKER exec onec-client cp "$f" /var/1C/licenses/
    done
    $DOCKER exec onec-client bash -c 'chmod 666 /var/1C/licenses/*.lic'
    echo "Лицензии лежат в ./licenses. Отправьте их на сервер: ./license.sh push USER@HOST:/путь/1c-server/licenses/"
    ;;
  push)
    [ -n "$2" ] || die "укажите назначение: USER@HOST:/путь/к/1c-server/licenses/"
    ls licenses/*.lic >/dev/null 2>&1 || die "в ./licenses нет .lic файлов, сначала ./license.sh install"
    scp licenses/*.lic "$2" || die "scp не удался"
    echo "Отправлено. На сервере выполните: sudo docker restart onec-server"
    ;;
  status)
    ls -la licenses
    ;;
  *) usage ;;
esac
