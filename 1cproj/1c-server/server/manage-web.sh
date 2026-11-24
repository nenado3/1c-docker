#!/bin/bash
set -Eeuo pipefail
BIN="/opt/1cv8/current"
APACHE_CONF="/etc/apache2/apache2.conf"
WEB_ROOT="/var/www/onec"
ACTION="${1:-status}"
NAME="${2:-}"
fail() { echo "ОШИБКА: $*" >&2; exit 1; }
apache_running() { ps -eo comm=,stat= | awk '$1 == "apache2" || $1 == "httpd" { if ($2 !~ /^Z/) found=1 } END { exit !found }'; }
apache_start() {
  apache2ctl configtest
  if apache_running; then
    echo "Apache уже запущен."
  else
    apache2ctl -k start
  fi
}
apache_reload() {
  apache2ctl configtest
  if apache_running; then
    apache2ctl -k graceful
  else
    apache2ctl -k start
  fi
}
valid_name() { [[ "$1" =~ ^[A-Za-z0-9_.-]{1,63}$ ]]; }
case "$ACTION" in
  start) apache_start ;;
  stop) apache2ctl -k stop ;;
  restart) apache_reload ;;
  status) if apache_running; then echo "Apache запущен."; else echo "Apache остановлен."; fi; apache2ctl -S ;;
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
      apache_reload
      echo "Веб-клиент опубликован: https://<IP-сервера>/$NAME"
    else
      echo "Удаление публикации '$NAME'..."
      "$WEBINST" -delete -apache24 -wsdir "$NAME" -confpath "$APACHE_CONF"
      rm -rf -- "$WEB_ROOT/$NAME"
      apache2ctl configtest
      apache_reload
      echo "Публикация '$NAME' удалена."
    fi
    ;;
  *) fail "использование: manage-web.sh {start|stop|restart|status|publish|delete} [имя-базы]" ;;
esac
