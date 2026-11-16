Сюда положите установщик платформы 1С той же версии, что и сервер:
  setup-full-<версия>-x86_64.run   (полный клиент + конфигуратор; нужен для ./export-db.sh)
  setup-thin-<версия>-x86_64.run   (только тонкий клиент)
или deb тонкого клиента: 1c-enterprise-<версия>-thin-client, -thin-client-nls и v8-install-deps.sh.
Пакеты common, server и client сюда не класть (thin-client конфликтует с common).
