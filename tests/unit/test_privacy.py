"""Приватность и разбор клиентских данных (``core/privacy.py``).

Всё здесь — чистые функции: ни Kafka, ни Redis не нужны. Раньше эти же свойства
проверялись через функциональный набор, то есть ценой поднятого брокера.
"""

import pytest

from core.privacy import (
    get_client_ip,
    hash_ip,
    is_trusted_proxy,
    parse_client_hints,
    truncate_user_agent,
)
from models.enums import DeviceType

CHROME_UA = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)
IPHONE_UA = (
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 '
    '(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'
)
GOOGLEBOT_UA = 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)'


class TestHashIp:
    def test_hash_is_stable(self):
        assert hash_ip('203.0.113.77') == hash_ip('203.0.113.77')

    def test_different_addresses_give_different_hashes(self):
        assert hash_ip('203.0.113.77') != hash_ip('203.0.113.78')

    def test_hash_does_not_contain_the_address(self):
        """Смысл хеширования — необратимость, а не переименование поля."""
        assert '203.0.113.77' not in hash_ip('203.0.113.77')

    def test_none_stays_none(self):
        """Отсутствие адреса не должно превращаться в хеш пустой строки:
        иначе все клиенты без адреса слились бы в одного «пользователя»."""
        assert hash_ip(None) is None
        assert hash_ip('') is None


class TestUserAgent:
    def test_long_user_agent_is_truncated(self):
        """Заголовок приходит от клиента и ничем не ограничен: без усечения
        одна строка раздула бы сообщение в Kafka до мегабайтов."""
        from core.config import settings

        result = truncate_user_agent('x' * 10_000)
        assert len(result) == settings.UGC_MAX_USER_AGENT_LENGTH

    def test_missing_user_agent(self):
        assert truncate_user_agent(None) is None

    @pytest.mark.parametrize(
        ('user_agent', 'expected'),
        [
            (CHROME_UA, DeviceType.DESKTOP),
            (IPHONE_UA, DeviceType.MOBILE),
            (GOOGLEBOT_UA, DeviceType.BOT),
            (None, DeviceType.UNKNOWN),
            ('полный мусор', DeviceType.UNKNOWN),
        ],
    )
    def test_device_type_detection(self, user_agent, expected):
        device_type, _, _ = parse_client_hints(user_agent)
        assert device_type is expected

    def test_os_and_browser_are_extracted(self):
        _, os_family, browser_family = parse_client_hints(CHROME_UA)
        assert os_family == 'Mac OS X'
        assert browser_family == 'Chrome'

    def test_unknown_families_become_none_not_other(self):
        """'Other' — внутреннее значение парсера, а не осмысленная категория:
        в хранилище оно выглядело бы как настоящая ОС."""
        _, os_family, browser_family = parse_client_hints('curl/8.0')
        assert os_family is None
        assert browser_family in (None, 'curl')


class TestTrustedProxy:
    @pytest.mark.parametrize('peer', ['172.18.0.5', '10.1.2.3', '192.168.1.1', '127.0.0.1'])
    def test_docker_and_private_ranges_are_trusted(self, peer):
        assert is_trusted_proxy(peer) is True

    @pytest.mark.parametrize('peer', ['203.0.113.10', '8.8.8.8', None, 'не-адрес'])
    def test_public_and_broken_addresses_are_not_trusted(self, peer):
        assert is_trusted_proxy(peer) is False


class TestGetClientIp:
    def test_real_ip_wins_over_forwarded_for(self):
        """X-Real-IP приоритетнее: его Nginx вычислил сам ($remote_addr после
        set_real_ip_from + real_ip_recursive), а левую часть XFF пишет клиент."""
        headers = {'x-forwarded-for': '1.2.3.4, 203.0.113.77', 'x-real-ip': '203.0.113.77'}
        assert get_client_ip(headers, '172.18.0.5') == '203.0.113.77'

    def test_last_hop_of_forwarded_for_wins(self):
        """Nginx дописывает реальный адрес СПРАВА ($proxy_add_x_forwarded_for),
        поэтому клиент — последний элемент, а не первый."""
        headers = {'x-forwarded-for': '10.0.0.1, 203.0.113.77'}
        assert get_client_ip(headers, '172.18.0.5') == '203.0.113.77'

    def test_forged_forwarded_for_prefix_is_ignored(self):
        """Главная проверка пункта: клиент подставляет свой XFF, Nginx дописывает
        к нему реальный адрес. Если бы сервис верил левому элементу, лимит по IP
        обходился бы сменой заголовка на каждый запрос, а ip_hash в аналитике
        накручивался бы произвольно."""
        forged = {'x-forwarded-for': '9.9.9.9, 203.0.113.77'}
        assert get_client_ip(forged, '172.18.0.5') == '203.0.113.77'

        # Сколько бы поддельных хопов клиент ни прислал — адрес тот же самый.
        many = {'x-forwarded-for': '1.1.1.1, 2.2.2.2, 3.3.3.3, 203.0.113.77'}
        assert get_client_ip(many, '172.18.0.5') == '203.0.113.77'

    def test_real_ip_is_used_when_forwarded_for_is_absent(self):
        headers = {'x-real-ip': '203.0.113.9'}
        assert get_client_ip(headers, '172.18.0.5') == '203.0.113.9'

    def test_peer_is_used_when_no_headers(self):
        assert get_client_ip({}, '172.18.0.5') == '172.18.0.5'

    def test_headers_from_untrusted_peer_are_ignored(self):
        """Клиент, обратившийся к сервису напрямую, не должен подменять свой
        адрес заголовком — иначе он получал бы новый счётчик rate limit на
        каждый запрос, то есть лимита бы не было."""
        headers = {'x-forwarded-for': '1.2.3.4', 'x-real-ip': '5.6.7.8'}
        assert get_client_ip(headers, '203.0.113.99') == '203.0.113.99'

    def test_empty_headers_fall_through_to_peer(self):
        """Пустой заголовок — не повод терять адрес: без него все клиенты
        слились бы в один счётчик 'unknown'."""
        headers = {'x-forwarded-for': '   ', 'x-real-ip': ''}
        assert get_client_ip(headers, '172.18.0.5') == '172.18.0.5'
