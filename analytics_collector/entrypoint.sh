#!/bin/sh
set -e

# Как и в auth/entrypoint.sh: если .env не пробросили, берём шаблон, чтобы
# сервис поднялся с разумными значениями по умолчанию.
if [ ! -f .env ]; then
  echo "Creating .env from .env.example"
  cp .env.example .env
fi

# Метрики из нескольких процессов uvicorn пишутся в mmap-файлы. Остатки от
# прошлого запуска нужно убрать: файлы умерших воркеров иначе продолжают
# отдаваться в скрейпе и показывают трафик, которого уже нет.
if [ -n "$PROMETHEUS_MULTIPROC_DIR" ]; then
  mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
  rm -f "$PROMETHEUS_MULTIPROC_DIR"/*.db 2>/dev/null || true
fi

exec "$@"
