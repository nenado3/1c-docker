# База: список пакетов ниже рассчитан на Ubuntu 24.04 (имена *t64).
# Для другого релиза нужно править список пакетов, поэтому BASE_IMAGE — только для совместимых образов.
ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}

ARG ONEC_VERSION=8.5.1.1522

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=ru_RU.UTF-8 \
    LANGUAGE=ru_RU:ru \
    LC_ALL=ru_RU.UTF-8 \
    ONEC_VERSION=${ONEC_VERSION} \
    APACHE_RUN_USER=usr1cv8 \
    APACHE_RUN_GROUP=grp1cv8

RUN apt-get update && apt-get install -y --no-install-recommends \
        locales ca-certificates fontconfig apache2 \
        libfreetype6 libglib2.0-0t64 libgsf-1-114 \
        unixodbc imagemagick \
        libxslt1.1 libcups2t64 libkrb5-3 libsecret-1-0 \
        libenchant-2-2 libstdc++6 \
    && sed -i 's/# ru_RU.UTF-8 UTF-8/ru_RU.UTF-8 UTF-8/' /etc/locale.gen \
    && locale-gen \
    && rm -rf /var/lib/apt/lists/*

# Библиотеки и виртуальный дисплей (xvfb) для выгрузки базы в .dt конфигуратором прямо в контейнере сервера.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgtk-3-0t64 libxss1 libxt6t64 xauth xvfb fonts-dejavu \
        libglu1-mesa libgl1 libwebkit2gtk-4.1-0 \
    && rm -rf /var/lib/apt/lists/*

RUN SYS_LIB="$(ldconfig -p | grep -m1 "libstdc++.so.6 " | sed -E "s/.*=>[[:space:]]*//")" \
    && test -n "$SYS_LIB" \
    && grep -aFq "GLIBCXX_3.4.30" "$SYS_LIB"

RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod +x /usr/sbin/policy-rc.d

RUN groupadd -g 1001 grp1cv8 \
    && useradd -m -u 1001 -g grp1cv8 usr1cv8 \
    && mkdir -p /home/usr1cv8/.1cv8 /var/log/1C /var/www/onec \
    && chown -R usr1cv8:grp1cv8 /home/usr1cv8 /var/log/1C /var/www/onec \
    && sed -i 's/^export APACHE_RUN_USER=.*/export APACHE_RUN_USER=usr1cv8/' /etc/apache2/envvars \
    && sed -i 's/^export APACHE_RUN_GROUP=.*/export APACHE_RUN_GROUP=grp1cv8/' /etc/apache2/envvars \\
    && printf 'ServerName localhost\\n' > /etc/apache2/conf-available/servername.conf \\
    && a2enconf servername

ARG ONEC_RUN_COMPONENTS=server,server_admin,client_full,ws,ru
COPY install-platform.sh /usr/local/bin/install-platform.sh
COPY dist/ /tmp/dist/
RUN chmod +x /usr/local/bin/install-platform.sh \
    && ONEC_REQUIRED_BIN="ragent ras rac ibcmd webinst" ONEC_OPTIONAL_BIN="1cv8" /usr/local/bin/install-platform.sh \
    && rm -rf /tmp/dist /var/lib/apt/lists/*

RUN set -eux; \
    PLATFORM_DIR="$(find /opt/1cv8 -maxdepth 2 -type d -name "${ONEC_VERSION}" -print -quit)"; \
    test -n "$PLATFORM_DIR"; \
    SYS_LIB="$(ldconfig -p | grep -m1 'libstdc++.so.6 ' | sed -E 's/.*=>[[:space:]]*//')"; \
    test -f "$SYS_LIB"; \
    grep -aFq 'GLIBCXX_3.4.30' "$SYS_LIB" \
        || { echo "Системная libstdc++ не содержит GLIBCXX_3.4.30: $SYS_LIB" >&2; exit 1; }; \
    if [ -e "$PLATFORM_DIR/libstdc++.so.6" ] || [ -L "$PLATFORM_DIR/libstdc++.so.6" ]; then \
        mv -f "$PLATFORM_DIR/libstdc++.so.6" "$PLATFORM_DIR/libstdc++.so.6.bundled"; \
    fi; \
    ln -s "$SYS_LIB" "$PLATFORM_DIR/libstdc++.so.6"; \
    echo "libstdc++ 1С заменена ссылкой на системную: $SYS_LIB"

COPY nethasp.ini /opt/1cv8/conf/nethasp.ini
RUN mkdir -p /var/1C/licenses /home/usr1cv8/backups && chmod 1777 /var/1C/licenses /home/usr1cv8/backups

COPY entrypoint.sh /entrypoint.sh
COPY 1c-xvfb /usr/local/bin/1c-xvfb
COPY manage-web.sh /usr/local/bin/manage-web.sh
RUN chmod +x /entrypoint.sh /usr/local/bin/1c-xvfb /usr/local/bin/manage-web.sh

WORKDIR /home/usr1cv8
EXPOSE 80 443 1540 1541 1545 1560-1591
ENTRYPOINT ["/entrypoint.sh"]
