#!/bin/bash
set -e

BIN="/opt/1cv8/current"

if [ ! -x "${BIN}/ragent" ]; then
    echo "ragent не найден в ${BIN}. Содержимое /opt/1cv8/:" >&2
    ls -laR /opt/1cv8/ >&2 || true
    exit 1
fi

if ls /var/1C/licenses/*.lic >/dev/null 2>&1; then
    echo "Найдены файлы лицензий:"; ls -la /var/1C/licenses/
else
    echo "ВНИМАНИЕ: в /var/1C/licenses нет .lic файлов. Получите лицензию клиентом (1c-client: ./license.sh activate)" >&2
fi

# RAS (сервер администрирования) в фоне.
# Кластер работает с -seclev 0, поэтому порт 1545 публикуется только на 127.0.0.1
# (см. docker-compose.yml) — иначе управление кластером было бы доступно в сети без пароля.
"${BIN}/ras" cluster --port=1545 &

# Агент сервера; сам запускает rmngr и rphost
exec "${BIN}/ragent" \
    -d /home/usr1cv8/.1cv8 \
    -port 1540 \
    -regport 1541 \
    -range 1560:1591 \
    -seclev 0 \
    -pingPeriod 1000 \
    -pingTimeout 5000
