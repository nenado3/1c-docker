#!/bin/bash
set -Eeuo pipefail
BIN="/opt/1cv8/current"
APACHE_CONF="/etc/apache2/apache2.conf"
WEB_ROOT="/var/www/onec"
ACTION="${1:-status}"
NAME="${2:-}"
fail() { echo "ОШИБКА: $*" >&2; exit 1; }
valid_name() { [[ "$1" =~ ^[A-Za-z0-9_.-]{1,63}$ ]]; }
case "$ACTION" in
  start) apache2ctl configtest; apache2ctl -k start ;;
  stop) apache2ctl -k stop ;;
  restart) apache2ctl configtest; apache2ctl -k graceful ;;
  status) apache2ctl -S ;;
  publish|delete)
    valid_name "$NAME" || fail "недопустимое имя публикации"
    WEBINST=""
    for candidate in "$BIN/webinst" /opt/1cv8/*/webinst; do
      if [[ -x "$candidate" ]]; then WEBINST="$candidate"; break; fi
    done
    [[ -n "$WEBINST" ]] || fail "webinst не найден. Проверьте, что компонент ws установлен в образе 1С."
    if [[ "$ACTION" == "publish" ]]; then
      mkdir -p "$WEB_ROOT/$NAME"
      chown -R usr1cv8:grp1cv8 "$WEB_ROOT/$NAME"
      echo "Публикация базы '$NAME' на Apache 2.4..."
      "$WEBINST" -publish -apache24 -wsdir "$NAME" -dir "$WEB_ROOT/$NAME" -connstr "Srvr=localhost;Ref=$NAME;" -confpath "$APACHE_CONF"
      chown -R usr1cv8:grp1cv8 "$WEB_ROOT/$NAME"
      apache2ctl configtest
      apache2ctl -k graceful || apache2ctl -k start
      echo "Веб-клиент опубликован: http://<IP-сервера>/$NAME"
    else
      echo "Удаление публикации '$NAME'..."
      "$WEBINST" -delete -apache24 -wsdir "$NAME" -confpath "$APACHE_CONF"
      rm -rf -- "$WEB_ROOT/$NAME"
      apache2ctl configtest
      apache2ctl -k graceful || apache2ctl -k start
      echo "Публикация '$NAME' удалена."
    fi
    ;;
  *) fail "использование: manage-web.sh {start|stop|restart|status|publish|delete} [имя-базы]" ;;
esac
