"""Транзакционный outbox: мост между базой и брокером.

## Зачем он вообще

Публиковать в брокер прямо из обработчика кнопки «Отправить» нельзя. Между
``COMMIT`` (рассылка получила статус «в очереди») и ``basic_publish`` есть окно, и
падение в этом окне даёт рассылку, которая по данным запущена, а по факту не
существует. Обратный порядок — сначала публикация, потом коммит — даёт то же
самое зеркально: пачка уходит в очередь для запуска, которого нет в базе.

Теория предлагает ровно это лекарство: «Если отправка сообщения не удалась,
сообщение лучше записать в какое-нибудь временное хранилище, а через некоторое
время попытаться переотправить заново». Разница лишь в том, что мы пишем в
хранилище ВСЕГДА и в той же транзакции, что и бизнес-данные, — тогда «не удалась»
перестаёт быть особым случаем.

## Три шага, а не одна длинная транзакция

Слив делает планировщик своим тиком, и каждая строка проходит **захват →
публикацию → фиксацию**. Транзакция открыта только на первом шаге и живёт
микросекунды; разговор с брокером идёт снаружи.

Раньше захват и публикация стояли внутри одной ``transaction.atomic()``, и
блокировка строки держалась всё время сетевого round-trip. Цена такого решения
видна не в коде, а под нагрузкой: ``heartbeat`` у соединения 600 секунд,
``blocked_connection_timeout`` — 300, и молчащий брокер означал открытую
транзакцию и занятое соединение из пула ровно столько же, на каждой реплике
планировщика. Хуже того, при выставленном ``idle_in_transaction_session_timeout``
Postgres убивал сессию посреди публикации и откатывал вместе с ней инкремент
``attempts`` — ядовитая строка не могла исчерпать потолок и загораживала очередь
вечно.

Взаимное исключение между репликами теперь держит не блокировка, а
``OutboxMessage.available_at``: захватив строку, реплика ставит его в будущее на
``NOTIFY_OUTBOX_CLAIM_TTL_SECONDS`` и отпускает транзакцию.

**Что аренда покрывает, а что нет.** Она нужна ровно на окно между
``basic_publish`` и записью ``published_at``: если процесс умрёт внутри него,
строку подберёт сосед — но не раньше, чем истечёт аренда. Дубль публикации в этом
окне возможен и безвреден: ``ScheduledRun.run_key`` и
``DeliveryTask.idempotency_key`` не дадут ему превратиться во второе письмо.
Именно поэтому аренда должна быть ДЛИННЕЕ самой долгой возможной публикации
(отношение к ``NOTIFY_AMQP_HEARTBEAT`` проверяется на старте): аренда, истекающая
пока первая реплика ещё в эфире, делает дубль штатным, а не аварийным.

**Предусловие:** ``drain`` обязан вызываться в autocommit, то есть не внутри
чужой ``transaction.atomic()``. Иначе «короткие» блоки выродятся в точки
сохранения, публикация снова окажется внутри транзакции вызывающего, и правка
молча откатится. Единственный вызывающий — цикл ``run_scheduler``.

## Почему у строки есть потолок попыток и отсрочка

``PublishFailed`` приходит по двум разным поводам, и теперь они РАЗЛИЧИМЫ: у
``Unroutable`` (у ключа маршрутизации нет ни одной привязки) лечения не бывает, а
``BrokerUnavailable`` лечится ожиданием. Разводить их обязательно, потому что
реакция противоположная: в первом случае брокер жив и надо идти к следующей
строке, во втором следующая упрётся в то же самое.

Потолок ``NOTIFY_OUTBOX_MAX_ATTEMPTS`` остаётся страховкой для ОБЕИХ
разновидностей — ``AMQPError`` покрывает и вечные ошибки вроде отказа
аутентификации, которые без бюджета крутились бы в горячем цикле навсегда. Но
сам по себе он мерил не выносливость, а секунды: слив крутится раз в секунду, и
десять попыток сгорали за десять секунд недоступности брокера, после чего
исправная строка выпадала из выборки навсегда (сбросить ``attempts`` можно только
руками в базе). Поэтому неудача не просто тратит попытку, а откладывает строку на
экспоненциальный backoff — те же десять попыток стоят теперь порядка получаса
терпимого отказа.

Арифметика отсрочки взята из ``practix_core.backoff.compute_delay`` — ровно та,
ради которой пять форков кривой задержки в репозитории и сводили в одну. Джиттер
здесь по прямому назначению из её докстринга: две реплики, поймавшие один отказ,
не должны ломиться в брокер синхронно.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import F, Q

from practix_core.backoff import compute_delay
from practix_core.context import get_request_id
from practix_notifications.broker import topology
from practix_notifications.broker.publisher import BrokerConnection, PublishFailed, Unroutable
from practix_notifications.campaigns.models import OutboxMessage
from practix_notifications.core.config import settings

logger = logging.getLogger('notifications.outbox')

#: Разброс отсрочки. Ровно тот случай, под который джиттер и заведён: две реплики
#: планировщика ловят один и тот же отказ брокера в одну и ту же секунду.
RETRY_JITTER = 0.2


def enqueue(*, exchange: str, routing_key: str, body: dict[str, Any], headers: dict[str, Any] | None = None) -> None:
    """Положить сообщение в очередь на публикацию.

    Вызывается ВНУТРИ транзакции вместе с бизнес-данными — в этом весь смысл.
    """
    payload = dict(headers or {})
    request_id = get_request_id()
    if request_id:
        payload.setdefault(topology.HEADER_REQUEST_ID, request_id)
    OutboxMessage.objects.create(exchange=exchange, routing_key=routing_key, body=body, headers=payload)


def drain(broker: BrokerConnection, *, limit: int) -> int:
    """Опубликовать накопившееся. Возвращает число отправленных сообщений.

    Каждая строка проходит захват → публикацию → фиксацию; транзакция открыта
    только на захвате. Подробности и предусловие — в докстринге модуля.
    """
    sent = 0
    for _ in range(limit):
        message = _claim()
        if message is None:
            break

        # Публикация. Транзакции здесь нет и быть не должно.
        try:
            broker.publish(
                exchange=message.exchange,
                routing_key=message.routing_key,
                body=message.body,
                headers=message.headers,
            )
        except Unroutable as exc:
            # Брокер жив, плоха именно эта строка. Идём дальше: раньше она
            # вставала в голову выборки по `created_at` и останавливала
            # публикацию ВСЕХ рассылок до исчерпания потолка попыток.
            _defer(message, exc)
            continue
        except PublishFailed as exc:
            # `BrokerUnavailable` и голый `PublishFailed`: следующие строки
            # упрутся в то же самое, а лог заполнится копиями одной ошибки.
            _defer(message, exc)
            return sent
        except Exception as exc:
            # Что угодно, кроме `PublishFailed`, — например `TypeError` из
            # `json.dumps` на несериализуемом теле. Раньше такое исключение
            # откатывало транзакцию вместе с инкрементом попыток, и строка
            # загораживала очередь вечно, роняя один и тот же traceback каждый
            # тик. Тратим попытку и отдаём исключение наверх — там `_safe`
            # планировщика его залогирует.
            _defer(message, exc)
            raise

        # Фиксация. Отдельный ограниченный UPDATE, а не `save()` по устаревшему
        # снимку: строка не заблокирована, и её мог перехватить сосед по
        # истёкшей аренде. `published_at__isnull=True` делает запись безобидной,
        # если он уже успел.
        OutboxMessage.objects.filter(pk=message.pk, published_at__isnull=True).update(published_at=datetime.now(UTC))
        sent += 1
    return sent


def _claim() -> OutboxMessage | None:
    """Забрать одну строку под аренду. Единственное место с открытой транзакцией.

    ``skip_locked`` позволяет держать две реплики планировщика: вторая просто
    пройдёт мимо строки, которую прямо сейчас захватывает первая, вместо того
    чтобы ждать на блокировке. Блокировка живёт ровно два запроса.
    """
    now = datetime.now(UTC)
    with transaction.atomic():
        message = (
            OutboxMessage.objects.select_for_update(skip_locked=True)
            .filter(published_at__isnull=True, attempts__lt=settings.NOTIFY_OUTBOX_MAX_ATTEMPTS)
            .filter(Q(available_at__isnull=True) | Q(available_at__lte=now))
            .order_by('created_at')
            .first()
        )
        if message is None:
            return None
        if message.available_at is not None and message.attempts == 0:
            # Отметка стояла, а попыток не было — значит предыдущий владелец не
            # дошёл ни до успеха, ни до неудачи, то есть умер внутри публикации.
            # Строка сейчас может уйти в брокер вторично, и это стоит записи в
            # лог. Признак бесплатный (лишних запросов нет) и намеренно узкий:
            # он ловит только смерть на ПЕРВОЙ попытке, зато не путает аварию с
            # обычной отсрочкой после неудачи, у которой попытки уже потрачены.
            logger.warning(
                'Outbox message %s: lease taken at %s expired, republishing (a scheduler died mid-publish?)',
                message.id,
                message.available_at,
            )
        message.available_at = now + timedelta(seconds=settings.NOTIFY_OUTBOX_CLAIM_TTL_SECONDS)
        message.save(update_fields=['available_at'])
        return message


def _defer(message: OutboxMessage, exc: Exception) -> None:
    """Потратить попытку и отложить строку до истечения backoff.

    ``F('attempts') + 1`` вместо чтения-изменения-записи: снимок ``message``
    сделан ДО публикации, которая могла длиться минуты.
    """
    attempts = message.attempts + 1
    delay = compute_delay(
        attempts,
        start=settings.NOTIFY_OUTBOX_RETRY_START,
        border=settings.NOTIFY_OUTBOX_RETRY_MAX,
        jitter=RETRY_JITTER,
    )
    OutboxMessage.objects.filter(pk=message.pk).update(
        attempts=F('attempts') + 1,
        last_error=str(exc)[:2000],
        available_at=datetime.now(UTC) + timedelta(seconds=delay),
    )
    if attempts >= settings.NOTIFY_OUTBOX_MAX_ATTEMPTS:
        logger.error(
            'Outbox message %s giving up after %s attempts (%s → %s): %s',
            message.id,
            attempts,
            message.exchange,
            message.routing_key,
            exc,
        )
    else:
        logger.warning('Outbox publish failed, retrying in %.1fs: %s', delay, exc)
