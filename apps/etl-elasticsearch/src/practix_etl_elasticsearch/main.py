import logging
import time

from practix_core.sentry import init_sentry
from practix_etl_elasticsearch.gears.genres.extractor import GenresExtractor
from practix_etl_elasticsearch.gears.genres.index import GENRES_INDEX_BODY
from practix_etl_elasticsearch.gears.genres.transform import GenresTransform
from practix_etl_elasticsearch.gears.loader.elastic import ElasticsearchLoader
from practix_etl_elasticsearch.gears.movies.extractor import PostgresExtractor
from practix_etl_elasticsearch.gears.movies.index import INDEX_BODY
from practix_etl_elasticsearch.gears.movies.transform import DataTransform
from practix_etl_elasticsearch.gears.persons.extractor import PersonsExtractor
from practix_etl_elasticsearch.gears.persons.index import PERSONS_INDEX_BODY
from practix_etl_elasticsearch.gears.persons.transform import PersonsTransform
from practix_etl_elasticsearch.lib.storage import JsonFileStorage, State
from practix_etl_elasticsearch.logger import setup_logging
from practix_etl_elasticsearch.settings import settings

logger = logging.getLogger(__name__)


class ETLProcess:
    """Координатор ETL: запускает Extract → Transform → Load."""

    def __init__(
        self,
        extractor: PostgresExtractor,
        transformer: DataTransform,
        loader: ElasticsearchLoader,
        sleep_seconds: float,
    ) -> None:
        self.extractor = extractor
        self.transformer = transformer
        self.loader = loader
        self.sleep_seconds = sleep_seconds

    def run_once(self) -> int:
        """Один проход: вычитать всё новое и загрузить. Вернуть число загруженных документов."""
        self.loader.ensure_index()
        total = 0
        for rows in self.extractor.extract():
            docs = self.transformer.transform(rows)
            self.loader.load(docs)
            self.extractor.commit(rows)
            total += len(docs)
        return total

    def run_forever(self) -> None:
        while True:
            try:
                count = self.run_once()
                logger.info('Цикл завершён, загружено документов: %s', count)
            except Exception:
                logger.exception('Непредвиденная ошибка в цикле ETL')
            time.sleep(self.sleep_seconds)


def build_etl() -> ETLProcess:
    storage = JsonFileStorage(settings.STATE_FILE_PATH)
    state = State(storage)
    extractor = PostgresExtractor(state, settings.BATCH_SIZE)
    transformer = DataTransform()
    loader = ElasticsearchLoader(
        settings.ELASTICSEARCH_HOST,
        settings.ELASTICSEARCH_PORT,
        settings.ELASTICSEARCH_INDEX,
        INDEX_BODY,
    )
    return ETLProcess(extractor, transformer, loader, settings.ETL_SLEEP_SECONDS)


def build_genres_etl() -> ETLProcess:
    storage = JsonFileStorage(settings.STATE_FILE_PATH)
    state = State(storage)
    extractor = GenresExtractor(state, settings.BATCH_SIZE)
    transformer = GenresTransform()
    loader = ElasticsearchLoader(
        settings.ELASTICSEARCH_HOST,
        settings.ELASTICSEARCH_PORT,
        settings.ELASTICSEARCH_GENRES_INDEX,
        GENRES_INDEX_BODY,
    )
    return ETLProcess(extractor, transformer, loader, settings.ETL_SLEEP_SECONDS)


def build_persons_etl() -> ETLProcess:
    storage = JsonFileStorage(settings.STATE_FILE_PATH)
    state = State(storage)
    extractor = PersonsExtractor(state, settings.BATCH_SIZE)
    transformer = PersonsTransform()
    loader = ElasticsearchLoader(
        settings.ELASTICSEARCH_HOST,
        settings.ELASTICSEARCH_PORT,
        settings.ELASTICSEARCH_PERSONS_INDEX,
        PERSONS_INDEX_BODY,
    )
    return ETLProcess(extractor, transformer, loader, settings.ETL_SLEEP_SECONDS)


def main() -> None:
    setup_logging()
    # Строго ПОСЛЕ setup_logging: LoggingIntegration вешается на обработку записей,
    # и именно она превращает logger.exception из блоков ниже в события. Без
    # неё авария в цикле остаётся строкой в stdout, которую никто не читает.
    # OpenTelemetry в этом образе нет — тег trace_id просто не проставится.
    init_sentry(
        enabled=settings.SENTRY_ENABLED,
        dsn=settings.SENTRY_DSN,
        service_name=settings.OTEL_SERVICE_NAME,
        environment=settings.SENTRY_ENVIRONMENT,
        release=settings.SENTRY_RELEASE,
        sample_rate=settings.SENTRY_SAMPLE_RATE,
        send_default_pii=settings.SENTRY_SEND_DEFAULT_PII,
    )
    movies_etl = build_etl()
    genres_etl = build_genres_etl()
    persons_etl = build_persons_etl()

    while True:
        try:
            movies_count = movies_etl.run_once()
            logger.info('Movies ETL: загружено документов: %s', movies_count)
        except Exception:
            logger.exception('Непредвиденная ошибка в цикле Movies ETL')

        try:
            genres_count = genres_etl.run_once()
            logger.info('Genres ETL: загружено документов: %s', genres_count)
        except Exception:
            logger.exception('Непредвиденная ошибка в цикле Genres ETL')

        try:
            persons_count = persons_etl.run_once()
            logger.info('Persons ETL: загружено документов: %s', persons_count)
        except Exception:
            logger.exception('Непредвиденная ошибка в цикле Persons ETL')

        time.sleep(settings.ETL_SLEEP_SECONDS)


if __name__ == '__main__':
    main()
