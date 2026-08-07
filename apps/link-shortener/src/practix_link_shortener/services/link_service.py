"""Создание, резолв и учёт визитов коротких ссылок."""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from practix_link_shortener.core.config import settings
from practix_link_shortener.models.entity import KIND_CONFIRM_EMAIL, ShortLink
from practix_link_shortener.services.codes import make_code
from practix_link_shortener.services.exceptions import CodeGenerationFailed
from practix_link_shortener.services.targets import validate_target

logger = logging.getLogger(__name__)

#: Имена ограничений из миграции. IntegrityError на одной и той же вставке может
#: означать РАЗНОЕ, и различать эти два случая обязательно: коллизию кода надо
#: перегенерировать, а повтор ключа идемпотентности — вернуть существующую
#: строку. Слепой повтор на любой IntegrityError крутился бы вечно на дубле ключа.
PK_CONSTRAINT = 'short_link_pkey'
IDEMPOTENCY_CONSTRAINT = 'short_link_idempotency_key_uq'

#: Счётчик визитов. `coalesce(first_visited_at, now())` сохраняет ПЕРВЫЙ переход:
#: последующие не переписывают историю. Колонка не проиндексирована, поэтому
#: обновление остаётся HOT — без записи в индекс и без раздувания.
_COUNT_VISIT = text("""
    UPDATE short_link
       SET visit_count = visit_count + 1,
           last_visited_at = now(),
           first_visited_at = coalesce(first_visited_at, now())
     WHERE code = :code
""")

#: Одна пачка уборки. Неограниченный `DELETE ... WHERE expires_at < :before` —
#: это одна длинная транзакция: на объёмах массовых рассылок она держит
#: блокировки на всех удаляемых строках (а по ним же ходит `_COUNT_VISIT`
#: горячего редиректа), раздувает WAL одним всплеском и откладывает autovacuum
#: до самого конца. Поэтому удаляем порциями, каждая — своя короткая транзакция.
#:
#: Подзапрос идёт по `short_link_expires_at_idx` — индексу, заведённому в
#: миграции ровно под уборщика, а не под горячий путь (тот ходит по PK).
#: `FOR UPDATE SKIP LOCKED` нужен, чтобы два одновременных уборщика не вставали
#: в очередь друг за другом на пересечении своих пачек.
_PURGE_BATCH = text("""
    DELETE FROM short_link
     WHERE code IN (
         SELECT code
           FROM short_link
          WHERE expires_at < :before
          ORDER BY expires_at
          LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
     )
""")


@dataclass(frozen=True, slots=True)
class CreatedLink:
    """Результат создания: сама ссылка и была ли она создана сейчас."""

    link: ShortLink
    created: bool


class LinkService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        *,
        target_url: str,
        kind: str,
        user_id: uuid.UUID | None = None,
        ttl_hours: int | None = None,
        idempotency_key: str | None = None,
    ) -> CreatedLink:
        """Завести короткую ссылку (или вернуть уже заведённую по тому же ключу)."""
        validated = validate_target(
            target_url,
            allowed_hosts=settings.allowed_redirect_hosts,
            self_host=settings.public_host,
            self_path_prefix=f'/{settings.SHORTENER_REDIRECT_PATH.strip("/")}/',
        )

        hours = min(ttl_hours or settings.SHORTENER_DEFAULT_TTL_HOURS, settings.SHORTENER_MAX_TTL_HOURS)
        expires_at = datetime.now(UTC) + timedelta(hours=hours)

        # Быстрый путь для повторного вызова: не тратим попытку вставки, когда
        # ключ уже известен. Гонку двух одновременных вызовов это не закрывает —
        # её ловит уникальный индекс ниже.
        if idempotency_key:
            existing = await self._by_idempotency_key(idempotency_key)
            if existing is not None:
                return CreatedLink(link=await self._revive(existing, expires_at), created=False)

        for attempt in range(settings.SHORTENER_CODE_MAX_ATTEMPTS):
            link = ShortLink(
                code=make_code(settings.SHORTENER_CODE_LENGTH),
                kind=kind,
                user_id=user_id,
                target_url=validated,
                idempotency_key=idempotency_key,
                expires_at=expires_at,
            )
            self.session.add(link)
            try:
                await self.session.commit()
            except IntegrityError as exc:
                await self.session.rollback()
                constraint = _constraint_name(exc)
                if constraint == IDEMPOTENCY_CONSTRAINT:
                    # Кто-то успел раньше с тем же ключом. Это и есть успех
                    # идемпотентности: возвращаем чужую строку, а не свою.
                    existing = await self._by_idempotency_key(idempotency_key) if idempotency_key else None
                    if existing is not None:
                        return CreatedLink(link=await self._revive(existing, expires_at), created=False)
                    raise
                if constraint != PK_CONSTRAINT:
                    # Незнакомое ограничение — не наш случай, повторять нечего.
                    raise
                logger.warning('Коллизия короткого кода, попытка %s', attempt + 1)
                continue
            return CreatedLink(link=link, created=True)

        raise CodeGenerationFailed(
            f'не удалось подобрать свободный код за {settings.SHORTENER_CODE_MAX_ATTEMPTS} попыток'
        )

    async def get(self, code: str) -> ShortLink | None:
        """Строка по коду. Один поиск по первичному ключу."""
        return await self.session.get(ShortLink, code)

    async def resolve(self, code: str) -> ShortLink | None:
        """Живая ссылка по коду, либо ``None``.

        «Нет такой», «протухла» и «отозвана» намеренно склеены в один ответ:
        различать их снаружи — значит подарить перебирающему оракул, который
        подсказывает, какие коды существуют.
        """
        link = await self.get(code)
        if link is None or link.revoked_at is not None:
            return None
        if link.expires_at <= datetime.now(UTC):
            return None
        return link

    async def count_visit(self, code: str) -> None:
        """Учесть переход.

        Вызывается ПОСЛЕ решения о редиректе и вне транзакции подтверждения:
        медленный счётчик не должен задерживать 302, а упавший — отменять уже
        случившееся подтверждение. Тот же приём и та же причина, что у
        ``emit_user_registered`` в Auth.
        """
        await self.session.execute(_COUNT_VISIT, {'code': code})
        await self.session.commit()

    async def purge_expired(self, *, before: datetime, batch_size: int | None = None) -> int:
        """Удалить ссылки, протухшие раньше указанного момента. Возвращает число строк.

        Идём пачками с коммитом на каждую: см. ``_PURGE_BATCH`` о том, почему
        одним запросом нельзя. Граница строгая (``<``), как и в ``resolve``:
        момент ``expires_at == before`` в выборку не входит.

        Цикл заведомо конечен, и ограничитель числа итераций тут был бы вреден —
        он молча оставил бы хвост. Пополнить выборку никто не может: ``before``
        зафиксирован на весь прогон, а ``expires_at`` новой строки всегда в
        будущем (``create`` считает его как ``now() + ttl``).
        """
        size = batch_size or settings.SHORTENER_PURGE_BATCH
        removed = 0
        while True:
            result = await self.session.execute(_PURGE_BATCH, {'before': before, 'batch_size': size})
            await self.session.commit()
            deleted = result.rowcount or 0
            removed += deleted
            # Выход по пустой пачке, а НЕ по неполной: со SKIP LOCKED неполная
            # означает «часть строк занята соседом», а не «строк больше нет».
            if deleted == 0:
                break
            if settings.SHORTENER_PURGE_SLEEP:
                await asyncio.sleep(settings.SHORTENER_PURGE_SLEEP)
        if removed:
            logger.info('Убрано протухших ссылок: %s (раньше %s)', removed, before.isoformat())
        return removed

    async def _revive(self, link: ShortLink, expires_at: datetime) -> ShortLink:
        """Продлить ссылку, если повтор пришёл после её смерти.

        Идемпотентность обязана возвращать ТУ ЖЕ ссылку — но «та же» не значит
        «мёртвая». Ключ идемпотентности здесь — ``DeliveryTask.idempotency_key``,
        то есть переотправка рассылки приходит с тем же ключом спустя дни: без
        продления в новое письмо легла бы ссылка, которая уже отвечает страницей
        404. Код при этом сохраняется, поэтому ранее разосланные письма
        продолжают работать, а не расщепляются на два адреса.

        Живую ссылку не трогаем: у неё уже есть срок, и сдвигать его при каждом
        обращении значило бы сделать TTL бесконечным.
        """
        now = datetime.now(UTC)
        if link.revoked_at is None and link.expires_at > now:
            return link

        logger.info('Продлеваем ссылку %s по повтору ключа идемпотентности', link.code)
        link.expires_at = expires_at
        # Отзыв тоже снимается: строку отозвали как «эту рассылку не шлём», а
        # повтор с тем же ключом — это решение выслать её снова.
        link.revoked_at = None
        await self.session.commit()
        return link

    async def _by_idempotency_key(self, idempotency_key: str) -> ShortLink | None:
        stmt = select(ShortLink).where(ShortLink.idempotency_key == idempotency_key)
        return (await self.session.execute(stmt)).scalar_one_or_none()


def _constraint_name(exc: IntegrityError) -> str | None:
    """Имя нарушенного ограничения из исключения драйвера.

    asyncpg кладёт его в ``constraint_name`` своего исключения. Разбирать текст
    сообщения нельзя: он локализуется и меняется между версиями PostgreSQL.
    """
    return getattr(exc.orig, 'constraint_name', None)


def build_short_url(code: str) -> str:
    """Внешний адрес ссылки — тот, что уедет в письмо."""
    return f'{settings.redirect_prefix}/{code}'


def needs_confirmation(link: ShortLink) -> bool:
    """Требует ли ссылка похода в Auth перед редиректом.

    Решает ВИД ССЫЛКИ, а не маршрут. Отдельный маршрут для подтверждения был бы
    дырой: достаточно постучаться в обычный, чтобы получить редирект, не
    подтвердив ничего.
    """
    return link.kind == KIND_CONFIRM_EMAIL
