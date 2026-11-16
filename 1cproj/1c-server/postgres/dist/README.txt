Сюда положите ВСЕ .deb PostgreSQL для 1С (amd64, одной версии):
libecpg6, libecpg-compat3, libpgtypes3, libpq5,
postgresql-NN, postgresql-client-NN,
postgresql-client-common, postgresql-common, postgresql-common-dev
Пакеты должны содержать расширения mchar и fulleq.

Дистрибутив в POSTGRES_BASE_IMAGE (.env) должен совпадать с тем, под который собраны
эти пакеты. Определить можно по зависимостям любого пакета:

    dpkg-deb -f postgresql-*.deb Depends

    libc6 (>= 2.43), libicu78, libxml2-16   -> ubuntu:26.04
    libc6 (>= 2.39), libicu74               -> ubuntu:24.04
    libc6 (>= 2.35), libicu70               -> ubuntu:22.04

Сборка сама сверяет версию libc6 и остановится с подсказкой, если база не та.
