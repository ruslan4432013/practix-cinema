"""Проверка целевого адреса — защита от открытого редиректа.

Сервис, который редиректит куда попало, — это готовый инструмент фишинга: в
адресной строке жертвы стоит НАШ домен, а уводит он на чужой. Проверять на
чтении поздно: ссылка уже в письме. Поэтому проверка на записи, один раз.

Чистый модуль: ни базы, ни настроек-синглтона. Всё, что нужно, приходит
аргументами — так его можно проверить таблицей случаев, а не стендом.
"""

from urllib.parse import urlsplit

from practix_link_shortener.services.exceptions import InvalidTargetUrl

#: Только http/https. `javascript:` — это XSS в адресной строке, `data:` —
#: подделка страницы, `file:` — попытка дотянуться до диска клиента.
ALLOWED_SCHEMES = frozenset({'http', 'https'})


def validate_target(url: str, *, allowed_hosts: frozenset[str], self_host: str, self_path_prefix: str) -> str:
    """Вернуть адрес, если по нему можно уводить; иначе бросить ``InvalidTargetUrl``.

    ``allowed_hosts`` — белый список, а не чёрный: перечислить всё плохое
    нельзя, а перечислить свои домены — можно.
    """
    candidate = url.strip()
    if not candidate:
        raise InvalidTargetUrl('целевой адрес пуст')

    parts = urlsplit(candidate)

    # Схема обязана быть явной. `//evil.com` — валидный protocol-relative URL:
    # браузер подставит текущую схему и уйдёт на чужой хост, а urlsplit вернёт
    # для него пустую схему, то есть проверка «схема в белом списке» его ловит.
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise InvalidTargetUrl(f'схема {parts.scheme!r} не разрешена, допустимы {sorted(ALLOWED_SCHEMES)}')

    # `parts.hostname` уже отбрасывает userinfo, поэтому `http://localhost@evil.com`
    # даёт hostname='evil.com' — именно тот хост, куда браузер и уйдёт. Сравнивать
    # с `parts.netloc` было бы ошибкой: там 'localhost@evil.com', и наивная
    # проверка «начинается с localhost» пропустила бы подделку.
    host = (parts.hostname or '').lower()
    if not host:
        raise InvalidTargetUrl('в целевом адресе нет хоста')
    if host not in allowed_hosts:
        raise InvalidTargetUrl(f'хост {host!r} не в белом списке SHORTENER_ALLOWED_REDIRECT_HOSTS')

    # Короткая ссылка на короткую ссылку — это цикл редиректов, который браузер
    # оборвёт ошибкой, а счётчик визитов накрутит.
    if host == self_host and parts.path.startswith(self_path_prefix):
        raise InvalidTargetUrl('целевой адрес — сама короткая ссылка, это зациклит редирект')

    return candidate
