# syntax=docker/dockerfile:1.7
#
# Отдельный Dockerfile — единственный среди сервисов, и это осознанно: сервис на
# Python 3.12 (uwsgi собирается из исходников против libpcre2), вне общего uv
# workspace и со своим uv.lock. Общий infra/docker/python-service.Dockerfile
# рассчитан на членов workspace и на 3.13.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpcre2-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /usr/local/bin/uv

WORKDIR /opt/app

# Форма ENV KEY=VALUE, а не legacy ENV KEY VALUE: вторая объявлена устаревшей и
# сыплет предупреждениями на каждой сборке.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UWSGI_PROCESSES=1 \
    UWSGI_THREADS=16 \
    UWSGI_HARAKIRI=240 \
    DJANGO_SETTINGS_MODULE=example.settings \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    VIRTUAL_ENV=/opt/app/.venv \
    PATH=/opt/app/.venv/bin:$PATH

# Манифесты отдельным слоем — он выживает при правке исходников.
# --locked: сборка падает на устаревшем локе. Прежний Dockerfile ставил из
# requirements.txt, который был скомпилирован сам из себя (`uv pip compile <файл>
# -o <тот же файл>`), без constraints — то есть воспроизводимости не было вовсе.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project

COPY run_uwsgi.sh run_uwsgi.sh
COPY uwsgi/uwsgi.ini uwsgi.ini

RUN mkdir -p /var/www/static/ /var/www/media/ /opt/app/static/ /opt/app/media/

COPY . .

EXPOSE 8000

ENTRYPOINT ["bash", "run_uwsgi.sh"]
