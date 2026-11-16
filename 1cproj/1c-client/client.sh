#!/bin/bash
. "$(dirname "$0")/common.sh"

NAME=onec-client

usage() {
    cat <<USAGE
Использование:
  ./client.sh                запустить 1С (окно со списком баз)
  ./client.sh <аргументы>    запустить 1С с аргументами, например:
                             ./client.sh ENTERPRISE /S "onec-server:1541\\mybase"
  ./client.sh check          проверить доступность сервера 1С по сети
  ./client.sh shell          открыть bash в клиентском контейнере
  ./client.sh up             запустить клиентский контейнер (без окна 1С)
  ./client.sh stop           остановить клиентский контейнер
  ./client.sh recreate       пересоздать контейнер (после смены SERVER_IP или правок compose)
USAGE
}

case "$1" in
    -h|--help) usage; exit 0 ;;
esac

[ -n "$SERVER_IP" ] || die "не задан SERVER_IP в .env (IP машины с 1c-server)"

case "$1" in
    stop)
        $DOCKER compose stop onec-client
        exit 0 ;;
    up)
        # запуск контейнера без открытия окна 1С (SERVER_IP, nethasp.ini и каталоги готовит common.sh)
        $DOCKER compose up -d onec-client || die "не удалось поднять onec-client (см. сообщение выше)"
        $DOCKER compose ps onec-client
        exit 0 ;;
    recreate)
        mkdir -p licenses && chmod 1777 licenses
        $DOCKER compose up -d --force-recreate onec-client
        exit $? ;;
    check)
        echo "Проверка сервера $SERVER_IP с этой машины:"
        FAIL=0
        for p in 1540 1541; do
            if timeout 3 bash -c "</dev/tcp/${SERVER_IP}/$p" 2>/dev/null; then
                echo "  порт $p: открыт"
            else
                echo "  порт $p: НЕДОСТУПЕН"; FAIL=1
            fi
        done
        # rphost поднимается при первом сеансе, поэтому закрытый 1560 без сеансов — норма
        for p in 1560 1561; do
            if timeout 3 bash -c "</dev/tcp/${SERVER_IP}/$p" 2>/dev/null; then
                echo "  порт $p: открыт"
            else
                echo "  порт $p: закрыт (rphost поднимается при первом сеансе — это нормально)"
            fi
        done
        if [ "$FAIL" = 1 ]; then
            echo "Проверьте: запущен ли ./start.sh на сервере, правильный ли SERVER_IP, не закрыты ли порты файрволом"
            echo "(на сервере открыть: 1540-1541/tcp и 1560-1591/tcp)."
            exit 1
        fi
        if $DOCKER ps --format '{{.Names}}' | grep -qx "$NAME"; then
            echo "Имя onec-server внутри клиентского контейнера:"
            $DOCKER exec "$NAME" getent hosts onec-server || echo "  не резолвится: выполните ./client.sh recreate"
        fi
        exit 0 ;;
    shell)
        # для консоли графическая сессия не нужна
        $DOCKER compose up -d onec-client >/dev/null || die "не удалось поднять onec-client"
        EXEC=($DOCKER exec -i)
        [ -t 0 ] && EXEC+=(-t)
        exec "${EXEC[@]}" "$NAME" /bin/bash ;;
esac

[ -n "$DISPLAY" ] || die "переменная DISPLAY пуста: нужна графическая сессия (не чистый SSH)"

mkdir -p licenses && chmod 1777 licenses

if command -v xhost >/dev/null 2>&1; then
    xhost +local:docker >/dev/null
else
    echo "xhost не найден, окно может не открыться (пакет xorg-x11-server-utils / x11-xserver-utils)"
fi

# Тот же контейнер: создаётся при первом запуске, затем только запускается
$DOCKER compose up -d onec-client || die "не удалось поднять onec-client"

EXEC=($DOCKER exec -i -e DISPLAY="$DISPLAY" -e NO_AT_BRIDGE=1)
[ -t 0 ] && EXEC+=(-t)

exec "${EXEC[@]}" "$NAME" "${ONEC_BIN}/1cv8c" "$@"
