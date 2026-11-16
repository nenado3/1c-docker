# ВАЖНО: база должна совпадать с дистрибутивом, под который собраны deb-пакеты PostgreSQL от 1С.
# Определяется по зависимостям пакетов: например libc6 >= 2.43 / libicu78 / libxml2-16 — это Ubuntu 26.04,
# libc6 2.39 / libicu74 — 24.04, libicu70 — 22.04. Задаётся в .env: POSTGRES_BASE_IMAGE=ubuntu:26.04
ARG BASE_IMAGE=ubuntu:26.04
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=ru_RU.UTF-8 \
    LANGUAGE=ru_RU:ru \
    LC_ALL=ru_RU.UTF-8 \
    PGDATA=/var/lib/postgresql/data

# Локали ru_RU и en_US (1С выполняет SET lc_messages to 'en_US.UTF-8')
RUN apt-get update && apt-get install -y --no-install-recommends \
        locales ca-certificates tzdata \
    && sed -i 's/# ru_RU.UTF-8 UTF-8/ru_RU.UTF-8 UTF-8/; s/# en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen \
    && locale-gen \
    && rm -rf /var/lib/apt/lists/*

# В контейнере нет systemd: запрещаем автозапуск служб из postinst
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod +x /usr/sbin/policy-rc.d

# Не создавать кластер "main" при установке postgresql-common
RUN mkdir -p /etc/postgresql-common \
    && printf 'create_main_cluster = false\n' > /etc/postgresql-common/createcluster.conf

# Установка локальных deb-пакетов PostgreSQL для 1С.
# Перед установкой сверяем требуемую пакетами версию libc6 с версией в образе: при несовпадении
# apt выдаёт длинную портянку зависимостей, а нам нужен один понятный совет.
COPY dist/ /tmp/dist/
RUN set -eux; \
    ls -la /tmp/dist; \
    ARCH=$(dpkg --print-architecture); \
    mkdir -p /tmp/pkgs; \
    for f in /tmp/dist/*.deb; do \
        A=$(dpkg-deb -f "$f" Architecture); \
        if [ "$A" = "$ARCH" ] || [ "$A" = "all" ]; then \
            cp "$f" /tmp/pkgs/; \
        else \
            echo "Пропускаю $(basename "$f") (архитектура $A, образ для $ARCH)"; \
        fi; \
    done; \
    ls /tmp/pkgs/*.deb >/dev/null 2>&1 || { echo "ОШИБКА: в dist нет пакетов для архитектуры $ARCH"; exit 1; }; \
    REQ_LIBC=$(for f in /tmp/pkgs/*.deb; do dpkg-deb -f "$f" Depends || true; done \
        | tr ',' '\n' | sed -n 's/.*libc6 (>= *\([^) ]*\)).*/\1/p' | sort -V | tail -n1); \
    CUR_LIBC=$(dpkg-query -W -f='$${Version}' libc6); \
    if [ -n "$REQ_LIBC" ] && ! dpkg --compare-versions "$CUR_LIBC" ge "$REQ_LIBC"; then \
        echo "ОШИБКА: deb-пакеты 1С требуют libc6 >= $REQ_LIBC, в базовом образе $CUR_LIBC." >&2; \
        echo "Задайте в .env POSTGRES_BASE_IMAGE с вашим дистрибутивом (например ubuntu:26.04)." >&2; \
        exit 1; \
    fi; \
    apt-get update; \
    apt-get install -y /tmp/pkgs/*.deb || { \
        echo "ОШИБКА: apt не смог установить пакеты PostgreSQL." >&2; \
        echo "Проверьте POSTGRES_BASE_IMAGE: он должен совпадать с дистрибутивом ваших deb-пакетов 1С." >&2; \
        exit 1; \
    }; \
    rm -rf /tmp/dist /tmp/pkgs /var/lib/apt/lists/*

RUN set -eux; \
    id postgres >/dev/null 2>&1 || useradd -m -s /bin/bash postgres; \
    INITDB=$(find /usr/lib/postgresql /opt -name initdb -type f 2>/dev/null | head -n1); \
    [ -n "$INITDB" ] || { echo "ОШИБКА: initdb не найден, пакет postgresql не установился"; exit 1; }; \
    ln -sfn "$(dirname "$INITDB")" /usr/local/pgbin; \
    echo "PGBIN -> $(dirname "$INITDB")"; \
    "$INITDB" --version; \
    MCHAR=$(find / -name mchar.control 2>/dev/null | head -n1); \
    [ -n "$MCHAR" ] || { echo "ОШИБКА: расширение mchar не найдено в этих пакетах"; exit 1; }; \
    echo "mchar: $MCHAR"; \
    find / -name fulleq.control 2>/dev/null | head -n1 || true; \
    rm -rf /var/lib/postgresql/*/main 2>/dev/null || true; \
    mkdir -p "$PGDATA"; \
    chown -R postgres:postgres /var/lib/postgresql; \
    chmod 700 "$PGDATA"

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 5432
VOLUME /var/lib/postgresql/data
ENTRYPOINT ["/entrypoint.sh"]
