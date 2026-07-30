FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/opt/app/auth/src

WORKDIR /opt/app/auth

COPY auth/requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

COPY auth/ .

RUN chmod +x entrypoint.sh

ENTRYPOINT ["/opt/app/auth/entrypoint.sh"]

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
