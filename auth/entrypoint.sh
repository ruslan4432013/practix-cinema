#!/bin/sh

# Если .env отсутствует, создаем его из .env.example
if [ ! -f .env ]; then
  echo "Creating .env from .env.example"
  cp .env.example .env
fi

# Выполняем команду, переданную в CMD
exec "$@"
