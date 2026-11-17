#!/bin/bash
cd "$(dirname "$0")"

# Если пользователь в группе docker: DOCKER=docker ./client.sh
DOCKER="${DOCKER:-sudo docker}"

# Версия и SERVER_IP берутся из .env
set -a; . ./.env; set +a

# /opt/1cv8/current создаётся при сборке образа (не зависит от архитектуры)
ONEC_BIN="/opt/1cv8/current"

die() { echo "ОШИБКА: $*" >&2; exit 1; }

# SERVER_IP может быть именем хоста: переводим в IP (extra_hosts в Docker принимает только IP)
if [ -n "$SERVER_IP" ] && ! [[ "$SERVER_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && [[ "$SERVER_IP" != *:* ]]; then
    RESOLVED=$(getent ahostsv4 "$SERVER_IP" | awk 'NR==1{print $1}')
    [ -n "$RESOLVED" ] || die "не удалось определить IP для '$SERVER_IP': впишите числовой IP в .env"
    echo "SERVER_IP: $SERVER_IP -> $RESOLVED"
    SERVER_IP="$RESOLVED"
    export SERVER_IP
fi

# Каталоги, которые монтируются в контейнер (создаём заранее, иначе Docker создаст их от root)
EXPORT_DIR="${EXPORT_DIR:-./export}"
mkdir -p licenses "$EXPORT_DIR" && chmod 1777 licenses && chmod 777 "$EXPORT_DIR" 2>/dev/null || true
# backup-подпапка для выгрузок .dt/.cf (видна на веб-панели вместе со вложенными папками)
BACKUP_DIR="${EXPORT_DIR%/}/backup"
mkdir -p "$BACKUP_DIR" "$BACKUP_DIR/dt" "$BACKUP_DIR/cf" && chmod 777 "$BACKUP_DIR" "$BACKUP_DIR/dt" "$BACKUP_DIR/cf" 2>/dev/null || true

# nethasp.ini из NETHASP_ADDRS: адреса через запятую, пробел или точку с запятой
gen_nethasp() {
    [ -n "${NETHASP_ADDRS:-}" ] || return 0
    local addrs
    addrs=$(echo "$NETHASP_ADDRS" | tr ' ;' ',,' | tr -s ',' | sed 's/^,//; s/,$//')
    [ -n "$addrs" ] || return 0
    cat > nethasp.ini <<NHEOF
; Настройки клиента сетевого ключа HASP (NetHASP) для 1С. Файл создаётся скриптами из NETHASP_ADDRS (.env)
[NH_COMMON]
NH_TCPIP = Enabled

[NH_TCPIP]
NH_SERVER_ADDR = $addrs
NH_USE_BROADCAST = Disabled
NHEOF
}
gen_nethasp
[ -f nethasp.ini ] || die "нет nethasp.ini: задайте NETHASP_ADDRS в .env"
