# syntax=docker/dockerfile:1.7
#
# Один файл на все шесть Python-сервисов. До этого было пять Dockerfile'ов, из
# которых четыре — один шаблон с подставленными значениями, с побайтово
# одинаковой четырёхстрочной шапкой. Ни один не использовал multi-stage, кеш
# BuildKit или общий базовый слой, поэтому каждый сервис заново ставил весь свой
# набор зависимостей.
#
# Общий базовый ОБРАЗ здесь сознательно не используется: BuildKit и так
# переиспользует стадию `deps` между `--target`ами в рамках одной сборки, а
# отдельный базовый образ пришлось бы публиковать в реестр (которого нет) и
# версионировать, плюс он создал бы порядок сборки, который Nx должен был бы
# знать. Стадия в этом же файле даёт ту же экономию без обоих осложнений.
#
# Контекст сборки — КОРЕНЬ репозитория (как и раньше у четырёх из пяти), поэтому
# семантика .dockerignore не меняется на середине переезда.

ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.11.16

# ---------------------------------------------------------------- base --------
FROM python:${PYTHON_VERSION}-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # copy вместо hardlink: кеш uv лежит на другом слое, и hardlink между ними
    # невозможен — uv иначе печатает предупреждение на каждую установку.
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    # Интерпретатор берём из образа, а не качаем: иначе в слой попадёт второй
    # Python, и образ вырастет на ~50 МБ без всякой пользы.
    UV_PYTHON_DOWNLOADS=never \
    VIRTUAL_ENV=/opt/app/.venv \
    PATH=/opt/app/.venv/bin:$PATH
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /usr/local/bin/uv
WORKDIR /opt/app

# ---------------------------------------------------------------- deps --------
# Стадия общая для всех сервисов: её результат переиспользуется между --target.
FROM base AS deps
ARG PACKAGE
ARG UV_GROUPS=""

# ТОЛЬКО манифесты. Этот слой обязан выживать при любой правке исходников —
# иначе каждое изменение кода снова ставит все зависимости.
#
# Перечисление НЕЛЬЗЯ заменить на `COPY apps/*/pyproject.toml apps/`: COPY с
# подстановкой уплощает дерево, и все файлы схлопнутся в один. Список сверяется
# с tool.uv.workspace.members проверкой в CI (tools/check_docker_manifests.py).
COPY pyproject.toml uv.lock ./
COPY libs/platform-core/pyproject.toml        libs/platform-core/
COPY libs/analytics-contracts/pyproject.toml  libs/analytics-contracts/
COPY libs/search-schema/pyproject.toml        libs/search-schema/
COPY libs/testing/pyproject.toml              libs/testing/
COPY apps/movies-api/pyproject.toml           apps/movies-api/
COPY apps/auth/pyproject.toml                 apps/auth/
COPY apps/analytics-collector/pyproject.toml  apps/analytics-collector/
COPY apps/etl-clickhouse/pyproject.toml       apps/etl-clickhouse/
COPY apps/etl-elasticsearch/pyproject.toml    apps/etl-elasticsearch/
COPY apps/ugc-api/pyproject.toml              apps/ugc-api/

# --locked: сборка ПАДАЁТ на устаревшем локе. Это и есть замена никогда не
# существовавшему constraints.txt — утверждение, проверяемое на сборке, а не
# комментарий в заголовке файла.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-workspace --package "${PACKAGE}" ${UV_GROUPS}

# Исходники — отдельным слоем, после зависимостей.
COPY libs/ libs/
COPY apps/ apps/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --package "${PACKAGE}" ${UV_GROUPS}

# ------------------------------------------------------------- runtime --------
FROM base AS runtime
ARG APP_UID=1001
COPY --from=deps /opt/app/.venv /opt/app/.venv
COPY --from=deps /opt/app/libs /opt/app/libs
COPY --from=deps /opt/app/apps /opt/app/apps
# .env.example раньше приезжал вместе с `COPY <svc>/ .`; теперь нужен явный COPY.
# Без него entrypoint не сможет создать .env, и Auth упадёт на ValidationError
# ещё при импорте: у его настроек есть обязательные поля без значений.
COPY .env.example ./.env.example
COPY infra/docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
 && groupadd --system --gid ${APP_UID} appuser \
 && useradd  --system --uid ${APP_UID} --gid appuser appuser \
 && chown -R appuser:appuser /opt/app
# Непривилегированный пользователь теперь и у movies-api с auth: раньше они
# работали от root и без healthcheck, и это расхождение прятала копипаста.
USER appuser
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# ------------------------------------------------------ per-service leaves ----
FROM runtime AS movies-api
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=12 \
  CMD python -c "import sys,urllib.request as u; r=u.Request('http://127.0.0.1:8000/api/openapi.json', headers={'X-Request-Id':'healthcheck'}); sys.exit(0 if u.urlopen(r,timeout=3).status==200 else 1)"
# X-Request-Id в пробе обязателен: RequestIdMiddleware отвечает 400 без него, и
# контейнер никогда не стал бы healthy.
CMD ["uvicorn", "practix_movies_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "8"]

FROM runtime AS auth
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=12 \
  CMD python -c "import sys,urllib.request as u; r=u.Request('http://127.0.0.1:8000/api/openapi.json', headers={'X-Request-Id':'healthcheck'}); sys.exit(0 if u.urlopen(r,timeout=3).status==200 else 1)"
CMD ["uvicorn", "practix_auth.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS analytics-collector
# Метрики нескольких воркеров uvicorn складываются в mmap-файлы общего каталога.
ENV PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus_multiproc
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
  CMD python -c "import sys,urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/health/live',timeout=3).status==200 else 1)"
CMD ["uvicorn", "practix_analytics_collector.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]

FROM runtime AS etl-clickhouse
# PROMETHEUS_MULTIPROC_DIR здесь НЕ задаётся намеренно: сервис однопроцессный, а
# multiproc-режим отключает ProcessCollector и GCCollector — ровно те коллекторы,
# ради которых метрики памяти здесь и нужны.
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
  CMD python -c "import sys,urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/metrics',timeout=3).status==200 else 1)"
CMD ["python", "-m", "practix_etl_clickhouse.main"]

FROM runtime AS etl-elasticsearch
# Ни EXPOSE, ни HEALTHCHECK: сервис не слушает порт, это разовый цикл синхронизации.
#
# Каталог под STATE_FILE_PATH создаётся В ОБРАЗЕ и заранее отдаётся appuser.
# Иначе именованный том etl_state, который compose монтирует в /var/lib/etl,
# инициализируется правами несуществующего в образе каталога — root:root, — и
# процесс под uid 1001 не может записать состояние. Отказ при этом тихий:
# первая пачка грузится, `commit` падает с PermissionError, внешний `except`
# в цикле его проглатывает, и каждый следующий проход начинается с нуля. Синхронизация
# навсегда останавливается на первой пачке (100 персон из 4166), а сервис
# остаётся «живым». Docker переносит владельца каталога образа на пустой том —
# ровно поэтому chown обязан быть здесь, а не в entrypoint.
USER root
RUN mkdir -p /var/lib/etl && chown appuser:appuser /var/lib/etl
USER appuser
CMD ["python", "-m", "practix_etl_elasticsearch.main"]

FROM runtime AS ugc-api
# Проба идёт на /health/live, а не на openapi.json: она дешевле и не зависит от
# базы — перезапуск контейнера не чинит упавший PostgreSQL. X-Request-Id в пробе
# всё равно обязателен: RequestIdMiddleware работает в режиме reject_400 и без
# заголовка вернул бы 400 даже на health.
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=12 \
  CMD python -c "import sys,urllib.request as u; r=u.Request('http://127.0.0.1:8000/health/live', headers={'X-Request-Id':'healthcheck'}); sys.exit(0 if u.urlopen(r,timeout=3).status==200 else 1)"
CMD ["uvicorn", "practix_ugc_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
