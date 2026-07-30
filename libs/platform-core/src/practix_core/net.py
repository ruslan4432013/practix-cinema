"""Определение адреса клиента за обратным прокси.

Функция существовала в двух ПРОТИВОРЕЧАЩИХ вариантах, и это была не стилистика,
а уязвимость: ``auth`` брал ПЕРВЫЙ хоп ``X-Forwarded-For``, коллектор —
последний, и коллектор в комментарии подробно объяснял, почему первый неверен.

Nginx формирует заголовок как ``$proxy_add_x_forwarded_for`` — это присланный
клиентом XFF ПЛЮС дописанный справа реальный адрес. Слева, таким образом, стоит
ровно то, что подставил сам клиент. Беря первый элемент, сервис доверял бы
подделке: атакующий менял заголовок на каждый запрос и получал свежий счётчик
rate limit, то есть защита от брутфорса пароля обходилась одним заголовком.

Правильный порядок: ``X-Real-IP``, затем ПОСЛЕДНИЙ элемент ``X-Forwarded-For``,
и лишь затем адрес соединения. Nginx уже вычислил настоящий адрес
(``set_real_ip_from`` + ``real_ip_recursive`` отбрасывают доверенные хопы), и
результат попадает и в ``X-Real-IP``, и в правый край XFF.

ЗАГОЛОВКАМ ДОВЕРЯЕМ ТОЛЬКО ОТ ДОВЕРЕННОГО ПРОКСИ. Клиент, обратившийся к сервису
напрямую, иначе подделал бы заголовок и снова управлял бы своим ключом лимита.

Последствие обратной ошибки известно и учтено: если добавить перед Nginx ещё один
прокси и не внести его в ``set_real_ip_from``, все клиенты получат его адрес и
сольются в один счётчик. Это чинится настройкой доверенных сетей, а не возвратом
к первому элементу заголовка.

Модуль не импортирует настройки: сети передаются аргументом. Так одна и та же
функция обслуживает и lowercase-настройки Movies API, и UPPERCASE остальных.
"""

from collections.abc import Mapping, Sequence
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network

Network = IPv4Network | IPv6Network


def parse_networks(spec: str) -> list[Network]:
    """Разбирает список сетей из строки с запятыми.

    Невалидный элемент — ошибка конфигурации, а не повод молча сузить доверие:
    тихо отброшенная сеть означала бы, что все клиенты внезапно выглядят одним
    адресом и делят один счётчик лимита. Поэтому ``ip_network`` бросает наружу.
    """
    networks: list[Network] = []
    for item in spec.split(','):
        candidate = item.strip()
        if candidate:
            networks.append(ip_network(candidate))
    return networks


def is_trusted_proxy(peer: str | None, networks: Sequence[Network]) -> bool:
    """Пришло ли соединение от известного прокси.

    Пустой список доверенных сетей означает «доверять всем» и допустим только в
    тестах, где приложение поднимается in-process и адреса соединения нет.
    """
    if not networks:
        return True
    if not peer:
        return False
    try:
        address = ip_address(peer)
    except ValueError:
        return False
    return any(address in network for network in networks)


def resolve_client_ip(
    headers: Mapping[str, str],
    peer: str | None,
    networks: Sequence[Network],
) -> str | None:
    """Возвращает адрес клиента: ``X-Real-IP`` -> последний хоп XFF -> адрес соединения.

    ``headers`` должен позволять регистронезависимый доступ (у Starlette и httpx
    так и есть) либо содержать ключи в нижнем регистре.
    """
    if not is_trusted_proxy(peer, networks):
        return peer

    real_ip = (headers.get('x-real-ip') or '').strip()
    if real_ip:
        return real_ip

    forwarded_for = headers.get('x-forwarded-for') or ''
    hops = [hop.strip() for hop in forwarded_for.split(',') if hop.strip()]
    if hops:
        # Самый правый — тот, что дописал доверенный прокси. Всё левее прислал
        # клиент, и проверять это бессмысленно: он волен написать там что угодно.
        return hops[-1]
    return peer
