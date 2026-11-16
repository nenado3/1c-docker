#!/bin/bash
. "$(dirname "$0")/common.sh"

compgen -G "dist/*.deb" >/dev/null || compgen -G "dist/*.run" >/dev/null \
    || die "в dist нет ни .deb, ни .run"

$DOCKER compose build --progress=plain onec-client || die "сборка onec-client не удалась"
echo "Готово. Дальше: ./client.sh"
