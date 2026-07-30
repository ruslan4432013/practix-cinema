FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN pip install --upgrade pip && \
    pip install \
        "elasticsearch>=9.4.0" \
        "psycopg[binary]>=3.3.4" \
        "pydantic>=2.13.4" \
        "pydantic-settings>=2.14.0"

COPY etl ./etl

CMD ["python", "-m", "etl.main"]
