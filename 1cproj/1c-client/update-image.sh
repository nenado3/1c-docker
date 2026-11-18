#!/bin/bash
set -euo pipefail
. "$(dirname "$0")/common.sh"

echo "== Обновление Docker-образа клиента 1С"
echo "== Шаг 1/2: пересборка с обновлением базового образа"
$DOCKER compose build --pull --progress=plain onec-client
echo "== Шаг 2/2: пересоздание клиента"
$DOCKER compose up -d --force-recreate onec-client
echo "Обновление клиента завершено."
$DOCKER compose ps onec-client
