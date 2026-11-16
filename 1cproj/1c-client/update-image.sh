#!/bin/bash
set -euo pipefail
. "$(dirname "$0")/common.sh"

echo "== Обновление Docker-образа клиента 1С"
echo "== Шаг 1/2: получение обновлений базовых образов"
$DOCKER compose pull --ignore-buildable || echo "Предупреждение: образ собирается локально; продолжаем."
echo "== Шаг 2/2: пересборка и пересоздание клиента"
$DOCKER compose build --pull --progress=plain onec-client
$DOCKER compose up -d --force-recreate onec-client
echo "Обновление клиента завершено."
$DOCKER compose ps onec-client
