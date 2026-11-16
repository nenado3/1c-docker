Сюда положите установщик платформы 1С версии из .env:
  setup-full-<версия>-x86_64.run   (сервер + client_full для выгрузки .dt)
или deb одной версии:
  1c-enterprise-<версия>-common, -common-nls, -server, -server-nls, -client, -client-nls
и при наличии v8-install-deps.sh.
Пакеты thin-client сюда класть не нужно.
