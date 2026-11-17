# База: список пакетов ниже рассчитан на Ubuntu 22.04 (имена без *t64).
# Для другого релиза нужно править список пакетов, поэтому BASE_IMAGE — только для совместимых образов.
ARG BASE_IMAGE=ubuntu:22.04
FROM ${BASE_IMAGE}

ARG ONEC_VERSION=8.5.1.1522

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=ru_RU.UTF-8 \
    LANGUAGE=ru_RU:ru \
    LC_ALL=ru_RU.UTF-8 \
    ONEC_VERSION=${ONEC_VERSION}

RUN apt-get update && apt-get install -y --no-install-recommends \
        locales ca-certificates fontconfig fonts-dejavu \
        libfreetype6 libglib2.0-0 libgsf-1-114 \
        libgtk-3-0 libxss1 libxt6 xauth xvfb libglu1-mesa libgl1 \
        unixodbc imagemagick libxslt1.1 libcups2 \
        libkrb5-3 libsecret-1-0 libenchant-2-2 \
    && (apt-get install -y --no-install-recommends libwebkit2gtk-4.0-37 \
        || apt-get install -y --no-install-recommends libwebkit2gtk-4.1-0) \
    && sed -i 's/# ru_RU.UTF-8 UTF-8/ru_RU.UTF-8 UTF-8/' /etc/locale.gen \
    && locale-gen \
    && rm -rf /var/lib/apt/lists/*

RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod +x /usr/sbin/policy-rc.d

# Установка платформы из dist/ (.deb или .run) скриптом install-platform.sh:
# выбирает файлы своей архитектуры и версии ONEC_VERSION; для setup-full*.run подбирает допустимые
# компоненты по справке установщика, для setup-thin*.run флаг компонентов не передаёт.
ARG ONEC_RUN_COMPONENTS=client_full,client_thin,ru
COPY install-platform.sh /usr/local/bin/install-platform.sh
COPY dist/ /tmp/dist/
RUN chmod +x /usr/local/bin/install-platform.sh \
    && ONEC_REQUIRED_BIN="1cv8c" ONEC_OPTIONAL_BIN="1cv8" /usr/local/bin/install-platform.sh \
    && rm -rf /tmp/dist /var/lib/apt/lists/*

# Бандловая libstdc++ платформы старее системной и ломает WebKit
RUN if [ -f /opt/1cv8/current/libstdc++.so.6 ]; then \
        mv /opt/1cv8/current/libstdc++.so.6 /opt/1cv8/current/libstdc++.so.6.bak; \
    fi

# Ubuntu 22.04 (базовый образ клиента) предоставляет GLIBCXX_3.4.29; проверяем ABI-минимум WebKit 4.0.
RUN SYS_LIB="$(ldconfig -p | grep -m1 "libstdc++.so.6 " | sed -E "s/.*=>[[:space:]]*//")" \
    && test -n "$SYS_LIB" \
    && grep -aFq "GLIBCXX_3.4.29" "$SYS_LIB"

# Настройки сетевого ключа HASP (NetHASP): редактируйте nethasp.ini рядом с Dockerfile и пересоберите образ
# (или правьте его на хосте и пересоздавайте контейнер — файл смонтирован из docker-compose.yml)
COPY nethasp.ini /opt/1cv8/conf/nethasp.ini

# Диагностика: показать библиотеки, которых не хватает клиенту (сборку не останавливает)
RUN for b in 1cv8c 1cv8; do \
        [ -x /opt/1cv8/current/$b ] || continue; \
        echo "=== Недостающие библиотеки $b ==="; \
        (ldd /opt/1cv8/current/$b 2>&1 | grep "not found" || echo "все библиотеки найдены"); \
    done; echo "====================================="

# Обёртка для запуска конфигуратора без графической сессии (свой Xvfb вместо xvfb-run)
COPY 1c-xvfb /usr/local/bin/1c-xvfb
RUN chmod +x /usr/local/bin/1c-xvfb

# UID можно подогнать под пользователя хоста (CLIENT_UID в .env): нужен для доступа к X11
ARG USER_UID=1000
RUN useradd -m -u "${USER_UID}" user1c \
    && mkdir -p /var/1C/licenses && chmod 1777 /var/1C/licenses

USER user1c
WORKDIR /home/user1c

# Контейнер держится командой sleep infinity (см. compose), 1С запускается через ./client.sh
CMD ["sleep", "infinity"]
