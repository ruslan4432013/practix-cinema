#!/bin/sh
set -e

# Как и в analytics_collector/entrypoint.sh: если .env не пробросили, берём
# шаблон, чтобы сервис поднялся с разумными значениями по умолчанию.
if [ ! -f .env ]; then
  echo "Creating .env from .env.example"
  cp .env.example .env
fi

exec "$@"
