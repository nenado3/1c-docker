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

# Apache запускается от root, рабочие процессы — от usr1cv8 (см. /etc/apache2/envvars).
# Apache и файлы публикаций находятся в постоянных Docker volumes.
mkdir -p /var/www/onec /var/run/apache2 /var/lock/apache2
chown -R usr1cv8:grp1cv8 /var/www/onec
apache2ctl configtest
apache2ctl -k start

# RAS доступен только на localhost хоста через проброс 127.0.0.1:1545.
"${BIN}/ras" cluster --port=1545 &

# Агент сервера запускается от отдельного системного пользователя 1С.
exec runuser -u usr1cv8 -- "${BIN}/ragent" \
    -d /home/usr1cv8/.1cv8 \
    -port 1540 \
    -regport 1541 \
    -range 1560:1591 \
    -seclev 0 \
    -pingPeriod 1000 \
    -pingTimeout 5000
