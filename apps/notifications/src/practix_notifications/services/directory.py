"""Резолв личности получателя: из ``user_id`` — в имя, фамилию и адрес.

Единственное место, которое знает, откуда берутся личные данные. Источник —
сервис Auth, и это требование задания: воркер получает из очереди только
идентификатор и приходит за остальным сам.

## Почему не витрина подписчиков

Локальная таблица ``subscriber`` остаётся, но отвечает за другое: кого включать
в аудиторию, в какой таймзоне у него ночь и от чего он отписался. Личность —
имя, фамилия, актуальный адрес — берётся у владельца этих данных. Адрес,
застывший в очереди или в витрине на сутки, может уже принадлежать другому
человеку, а имени в витрине нет вовсе.

## Как это не убивает Auth

Чек-лист теории требует, чтобы «одновременная обработка большого количества
данных не приводила к отказу подсистемы данных о пользователях», и предлагает
две ручки — число консьюмеров и таймауты внутри каждого. Здесь:

* запрос идёт ПАЧКОЙ (``NOTIFY_BUILDER_LOOKUP_BATCH``), а не по получателю;
* между пачками — ``NOTIFY_BUILDER_SLEEP`` (по умолчанию 0, включается, когда
  начнёт мешать);
* повторный резолв тех же людей снимается кешом в памяти процесса.

Кеш держит ТОЛЬКО найденных. Промах не кешируется намеренно: пользователь,
зарегистрировавшийся через секунду после неудачного поиска, не должен оставаться
невидимым ещё пять минут — а именно ему и уходит приветственное письмо.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

from practix_notifications.core.config import settings
from practix_notifications.services.auth_client import AuthClient

logger = logging.getLogger('notifications.directory')


@dataclass(frozen=True)
class Person:
    """Личные данные получателя в том виде, в каком их ждёт шаблон."""

    user_id: str
    login: str
    email: str
    first_name: str
    last_name: str

    @property
    def full_name(self) -> str:
        """Имя и фамилия, а если их нет — логин.

        Пустая строка тут недопустима: обращение «Здравствуйте, !» хуже, чем
        обращение по логину.
        """
        full = f'{self.first_name} {self.last_name}'.strip()
        return full or self.login

    @property
    def display_name(self) -> str:
        """Как назвать человека в приветствии: имя, иначе — то же, что full_name."""
        return self.first_name or self.full_name

    @classmethod
    def from_auth(cls, raw: dict[str, Any]) -> 'Person':
        # None из Auth превращается в пустую строку прямо здесь: шаблонизатор
        # работает со StrictUndefined, а None отрендерился бы словом «None»
        # в теле письма.
        return cls(
            user_id=str(raw.get('id') or ''),
            login=str(raw.get('login') or ''),
            email=str(raw.get('email') or ''),
            first_name=str(raw.get('first_name') or ''),
            last_name=str(raw.get('last_name') or ''),
        )


class Directory:
    """Резолвер личности с пачками, притормаживанием и кешом.

    Один экземпляр на процесс воркера: вместе с ним живут токен сервисной учётки
    и кеш, иначе каждая пачка начиналась бы с повторного логина в Auth.
    """

    def __init__(self, client: AuthClient | None = None) -> None:
        self._client = client or AuthClient()
        self._cache: dict[str, tuple[float, Person]] = {}

    def resolve(self, subscriber_ids: list[str]) -> dict[str, Person]:
        """``{user_id: Person}`` для всех, кого знает Auth.

        Отсутствие человека в ответе — не ошибка: его могли удалить между
        синхронизацией витрины и рассылкой. Вызывающий отметит такую задачу как
        ``unknown_user``.

        Временная недоступность Auth пробрасывается наверх (``AuthUnavailable``):
        решение «подождать всей пачкой» принимает воркер, а не резолвер.
        """
        unique = list(dict.fromkeys(subscriber_ids))
        resolved: dict[str, Person] = {}
        misses: list[str] = []

        now = time.monotonic()
        for user_id in unique:
            cached = self._cached(user_id, now)
            if cached is not None:
                resolved[user_id] = cached
            else:
                misses.append(user_id)

        batch_size = settings.NOTIFY_BUILDER_LOOKUP_BATCH
        for index in range(0, len(misses), batch_size):
            chunk = misses[index : index + batch_size]
            found = self._client.lookup_users(chunk)
            for user_id, raw in found.items():
                person = Person.from_auth(raw)
                resolved[user_id] = person
                self._remember(user_id, person)
            # Между пачками, а не после последней: тормозить незачем, когда
            # работа уже кончилась.
            if settings.NOTIFY_BUILDER_SLEEP and index + batch_size < len(misses):
                time.sleep(settings.NOTIFY_BUILDER_SLEEP)

        logger.debug(
            'Resolved %d of %d recipients (%d from cache)', len(resolved), len(unique), len(unique) - len(misses)
        )
        return resolved

    def _cached(self, user_id: str, now: float) -> Person | None:
        if not settings.NOTIFY_DIRECTORY_CACHE_TTL:
            return None
        entry = self._cache.get(user_id)
        if entry is None:
            return None
        stored_at, person = entry
        if now - stored_at > settings.NOTIFY_DIRECTORY_CACHE_TTL:
            del self._cache[user_id]
            return None
        return person

    def _remember(self, user_id: str, person: Person) -> None:
        if not settings.NOTIFY_DIRECTORY_CACHE_TTL or not settings.NOTIFY_DIRECTORY_CACHE_MAX:
            return
        # Ограничение размера, а не «пусть растёт»: воркер живёт неделями, и
        # рассылка на миллион адресов не должна оставлять после себя миллион
        # записей в памяти. Выселяется самая старая — словарь помнит порядок
        # вставки.
        while len(self._cache) >= settings.NOTIFY_DIRECTORY_CACHE_MAX:
            self._cache.pop(next(iter(self._cache)))
        self._cache[user_id] = (time.monotonic(), person)


def template_vars(person: Person, *, unsubscribe_url: str, confirm_url: str = '') -> dict[str, str]:
    """Переменные шаблона, описывающие получателя.

    Все ключи присутствуют ВСЕГДА, пусть и пустыми строками: шаблонизатор
    работает со ``StrictUndefined``, и отсутствующая переменная — это не пустое
    место в письме, а неотправленное письмо. Поэтому ``confirm_url`` со
    значением по умолчанию: шаблон, который её не просит, не должен заставлять
    воркер ходить в сервис ссылок, но ключ обязан быть на месте.
    """
    return {
        'login': person.login,
        'email': person.email,
        'first_name': person.first_name,
        'last_name': person.last_name,
        'full_name': person.full_name,
        'display_name': person.display_name,
        'unsubscribe_url': unsubscribe_url,
        'confirm_url': confirm_url,
    }
