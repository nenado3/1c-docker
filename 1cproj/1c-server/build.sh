#!/bin/bash
. "$(dirname "$0")/common.sh"

# Необязательные аргументы: папки с установщиками (задаются в веб-панели, вкладка «Сборка»).
#   1 — папка с .run/.deb платформы 1С для сервера
#   2 — папка с .deb PostgreSQL для 1С
SRC_SERVER="${1:-}"
SRC_PG="${2:-}"

# Кладёт файлы из выбранной папки в dist/ (жёсткая ссылка, если та же ФС, иначе копия),
# чтобы Docker-контекст (./server, ./postgres) их увидел.
stage_dist() {
    local src="$1" dst="$2" f
    [ -n "$src" ] || return 0
    [ -d "$src" ] || die "нет папки: $src"
    mkdir -p "$dst"
    for f in "$src"/*.run "$src"/*.deb; do
        [ -f "$f" ] || continue
        rm -f "$dst/$(basename "$f")"
        ln "$f" "$dst/$(basename "$f")" 2>/dev/null || cp -f "$f" "$dst/" \
            || die "не удалось положить $(basename "$f") в $dst"
        echo "  $dst/$(basename "$f")"
    done
}

if [ -n "$SRC_SERVER" ] || [ -n "$SRC_PG" ]; then
    echo "== Подстановка установщиков из выбранных папок"
    stage_dist "$SRC_SERVER" server/dist
    stage_dist "$SRC_PG" postgres/dist
fi

compgen -G "postgres/dist/*.deb" >/dev/null || die "в postgres/dist нет .deb (укажите папку с пакетами PostgreSQL)"
compgen -G "server/dist/*.deb" >/dev/null || compgen -G "server/dist/*.run" >/dev/null \
    || die "в server/dist нет ни .deb, ни .run (укажите папку с установщиком 1С)"

echo "== Сборка postgres"
$DOCKER compose build --progress=plain postgres || die "сборка postgres не удалась"

echo "== Сборка сервера 1С"
$DOCKER compose build --progress=plain onec-server || die "сборка onec-server не удалась"

echo "Готово. Дальше: ./start.sh"
