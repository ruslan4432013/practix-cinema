import logging
import time

from etl.gears.genres.extractor import GenresExtractor
from etl.gears.genres.index import GENRES_INDEX_BODY
from etl.gears.genres.transform import GenresTransform
from etl.gears.loader.elastic import ElasticsearchLoader
from etl.gears.movies.extractor import PostgresExtractor
from etl.gears.movies.index import INDEX_BODY
from etl.gears.movies.transform import DataTransform
from etl.gears.persons.extractor import PersonsExtractor
from etl.gears.persons.index import PERSONS_INDEX_BODY
from etl.gears.persons.transform import PersonsTransform
from etl.lib.storage import JsonFileStorage, State
from etl.settings import settings

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
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
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
