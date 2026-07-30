# ETL Service

Сервис для переноса данных из PostgreSQL (`theatre-db`) в Elasticsearch. Он отслеживает изменения в базе данных и синхронизирует их с поисковым движком, обеспечивая актуальность данных для Movies API.

## Как это работает

`main.py` — координатор пайплайна. Он собирает три независимых «шестерёнки» (gears) — по одной на сущность — и в бесконечном цикле прогоняет каждую через `run_once()`:

| Gear | Пакет | Индекс ES |
|------|-------|-----------|
| Фильмы | `gears/movies/` | `ELASTICSEARCH_INDEX` |
| Жанры | `gears/genres/` | `ELASTICSEARCH_GENRES_INDEX` |
| Персоны | `gears/persons/` | `ELASTICSEARCH_PERSONS_INDEX` |

Каждая шестерёнка устроена по схеме **Extract → Transform → Load**:

- `extractor` (`PostgresExtractor` и т. п.) батчами (`BATCH_SIZE`) вычитывает из Postgres строки, изменённые с момента последней синхронизации;
- `transform` приводит строки к документам под маппинг ES;
- `index` содержит тело индекса (`INDEX_BODY`), которое загрузчик применяет при `ensure_index()`;
- `gears/loader/elastic.py` (`ElasticsearchLoader`) создаёт индекс при необходимости и грузит документы в ES.

**Инкрементальность.** Метки времени последней синхронизации хранятся в состоянии (`lib/storage.py`: `State` поверх `JsonFileStorage`), файл — по пути `STATE_FILE_PATH`. После загрузки батча `extractor.commit(rows)` продвигает метку, поэтому при перезапуске ETL продолжает с места остановки и держит ES в актуальном состоянии по мере изменений в Postgres.

**Надёжность.** `lib/backoff.py` обеспечивает повторные попытки при сбоях подключения. Ошибка в цикле одной шестерёнки логируется и не роняет остальные — цикл продолжается, а между проходами сервис спит `ETL_SLEEP_SECONDS`.

В Docker Compose ETL стартует после того, как `theatre-db` и `elasticsearch` станут healthy.

## Запуск локально

1. Убедитесь, что PostgreSQL и Elasticsearch запущены и доступны.
2. Установите зависимости (рекомендуется использовать виртуальное окружение):
   ```bash
   pip install -r requirements.txt
   ```
3. Настройте переменные окружения в файле `.env` в корне проекта или в директории `etl`.
4. Запустите ETL-процесс:
   ```bash
   python -m etl.main
   ```

## Переменные окружения

| Переменная | Описание | По умолчанию |
|------------|----------|--------------|
| `POSTGRES_DB` | Имя базы данных Postgres | (обязательно) |
| `POSTGRES_USER` | Имя пользователя Postgres | (обязательно) |
| `POSTGRES_PASSWORD` | Пароль пользователя Postgres | (обязательно) |
| `POSTGRES_HOST` | Хост БД | (обязательно) |
| `POSTGRES_PORT` | Порт БД | (обязательно) |
| `ELASTICSEARCH_HOST` | Хост Elasticsearch | `127.0.0.1` |
| `ELASTICSEARCH_PORT` | Порт Elasticsearch | `9200` |
| `ELASTICSEARCH_INDEX` | Индекс фильмов | `movies` |
| `ELASTICSEARCH_GENRES_INDEX` | Индекс жанров | `genres` |
| `ELASTICSEARCH_PERSONS_INDEX` | Индекс персон | `persons` |
| `BATCH_SIZE` | Количество записей за одну итерацию | `100` |
| `ETL_SLEEP_SECONDS` | Интервал между проверками обновлений | `10.0` |
| `STATE_FILE_PATH` | Путь к файлу состояния ETL | `etl_state.json` |
