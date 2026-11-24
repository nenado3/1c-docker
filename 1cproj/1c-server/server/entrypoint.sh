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

# Apache запускается от root, рабочие процессы — от usr1cv8.
# Конфигурация и автоматически созданный сертификат сохраняются в apache-config volume.
mkdir -p /var/www/onec /var/run/apache2 /var/lock/apache2 /etc/apache2/ssl
chown -R usr1cv8:grp1cv8 /var/www/onec

a2enmod ssl rewrite headers >/dev/null
a2dissite 000-default >/dev/null
# Persistent apache-config volumes can hide defaults from a newer image.
printf 'ServerName localhost\n' > /etc/apache2/conf-available/servername.conf
a2enconf servername >/dev/null
CERT_FILE="${HTTPS_CERT_FILE:-/etc/apache2/ssl/onec.crt}"
KEY_FILE="${HTTPS_KEY_FILE:-/etc/apache2/ssl/onec.key}"

if [[ -n "${HTTPS_CERT_FILE:-}" || -n "${HTTPS_KEY_FILE:-}" ]]; then
    if [[ ! -s "$CERT_FILE" || ! -s "$KEY_FILE" ]]; then
        echo "ОШИБКА: заданы HTTPS_CERT_FILE/HTTPS_KEY_FILE, но сертификат или ключ не найден." >&2
        exit 1
    fi
else
    if [[ ! -s "$CERT_FILE" || ! -s "$KEY_FILE" ]]; then
        echo "Создаю самоподписанный TLS-сертификат для Apache..."
        openssl req -x509 -nodes -newkey rsa:3072 -sha256 -days 825 \
            -keyout "$KEY_FILE" -out "$CERT_FILE" \
            -subj "/CN=onec-server" \
            -addext "basicConstraints=critical,CA:FALSE" \
            -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
            -addext "extendedKeyUsage=serverAuth" \
            -addext "subjectAltName=${HTTPS_CERT_SAN:-IP:192.168.239.130,IP:192.168.1.144,IP:127.0.0.1,DNS:localhost,DNS:onec-server,DNS:DESKTOP-MKJ07U7}"
        chmod 600 "$KEY_FILE"
        chmod 644 "$CERT_FILE"
    fi
fi

cat > /etc/apache2/conf-available/onec-https.conf <<APACHE_TLS
<VirtualHost *:80>
    ServerName localhost
    RewriteEngine On
    RewriteCond %{HTTP_HOST} ^(\[[0-9a-fA-F:]+\]|[A-Za-z0-9.-]+)(?::[0-9]+)?$
    RewriteRule ^ https://%1%{REQUEST_URI} [R=301,L,NE]
    RewriteRule ^ - [R=400,L]
</VirtualHost>

<VirtualHost *:443>
    ServerName localhost
    SSLEngine on
    SSLCertificateFile "$CERT_FILE"
    SSLCertificateKeyFile "$KEY_FILE"
    DocumentRoot /var/www/onec
    <Directory /var/www/onec>
        Options FollowSymLinks
        AllowOverride None
        Require all granted
    </Directory>
</VirtualHost>
APACHE_TLS

a2enconf onec-https >/dev/null
apache2ctl configtest
if [[ "${1:-}" == "--configure-apache-only" ]]; then
    exit 0
fi
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
