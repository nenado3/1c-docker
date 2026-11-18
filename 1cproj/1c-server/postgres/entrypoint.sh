#!/bin/bash
set -e

PGBIN=/usr/local/pgbin
PGDATA="${PGDATA:-/var/lib/postgresql/data}"
PASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set in .env}"
PGUSER_NAME="${POSTGRES_USER:-postgres}"

mkdir -p "$PGDATA"
chown postgres:postgres "$PGDATA"
chmod 700 "$PGDATA"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
    echo "Инициализация кластера PostgreSQL в $PGDATA"

    PWFILE=$(mktemp)
    echo "$PASSWORD" > "$PWFILE"
    chown postgres:postgres "$PWFILE"

    runuser -u postgres -- "$PGBIN/initdb" \
        -D "$PGDATA" \
        -U "$PGUSER_NAME" \
        --pwfile="$PWFILE" \
        -E UTF8 \
        --locale=ru_RU.UTF-8 \
        --auth-local=trust \
        --auth-host=md5
    rm -f "$PWFILE"

    {
        echo "listen_addresses = '*'"
        echo "max_connections = 200"
        echo "shared_buffers = 256MB"
        echo "standard_conforming_strings = off"
        echo "escape_string_warning = off"
    } >> "$PGDATA/postgresql.conf"

    echo "host all all 0.0.0.0/0 md5" >> "$PGDATA/pg_hba.conf"
fi

exec runuser -u postgres -- "$PGBIN/postgres" -D "$PGDATA"
