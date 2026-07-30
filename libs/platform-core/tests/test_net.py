"""Проверки определения адреса клиента за обратным прокси.

Это единственный кластер, где две копии функции ПРОТИВОРЕЧИЛИ друг другу, а не
просто дублировались: ``auth`` брал первый хоп ``X-Forwarded-For`` и был уязвим.
Главный тест здесь — ``test_spoofed_first_hop_is_ignored``: именно он падал бы на
прежней реализации Auth.
"""

import pytest

from practix_core.net import is_trusted_proxy, parse_networks, resolve_client_ip

# Диапазоны Docker и RFC1918 — то же значение, что в настройках обоих сервисов
# и в set_real_ip_from у Nginx.
TRUSTED = parse_networks('172.16.0.0/12,192.168.0.0/16,10.0.0.0/8,127.0.0.0/8')
NGINX = '172.18.0.5'


# --- parse_networks ---------------------------------------------------------


def test_parse_networks_handles_spaces_and_blanks():
    assert len(parse_networks(' 10.0.0.0/8 , ,192.168.0.0/16 ')) == 2


def test_parse_networks_empty_means_trust_everything():
    assert parse_networks('') == []
    assert parse_networks('  ,  ') == []


def test_parse_networks_raises_on_invalid_entry():
    """Тихо отбросить сеть нельзя: все клиенты слились бы в один счётчик."""
    with pytest.raises(ValueError):
        parse_networks('10.0.0.0/8,not-a-network')


def test_parse_networks_supports_ipv6():
    assert len(parse_networks('fd00::/8')) == 1


# --- is_trusted_proxy -------------------------------------------------------


@pytest.mark.parametrize('peer', ['172.18.0.5', '10.1.2.3', '192.168.1.1', '127.0.0.1'])
def test_trusted_peers(peer):
    assert is_trusted_proxy(peer, TRUSTED)


@pytest.mark.parametrize('peer', ['8.8.8.8', '203.0.113.1', None, '', 'garbage'])
def test_untrusted_peers(peer):
    assert not is_trusted_proxy(peer, TRUSTED)


def test_empty_network_list_trusts_everything():
    """Режим для тестов: приложение in-process, адреса соединения нет."""
    assert is_trusted_proxy(None, [])
    assert is_trusted_proxy('8.8.8.8', [])


# --- resolve_client_ip ------------------------------------------------------


def test_spoofed_first_hop_is_ignored():
    """РЕГРЕССИЯ НА УЯЗВИМОСТЬ. Прежний Auth возвращал бы '1.2.3.4'.

    Nginx строит заголовок как `$proxy_add_x_forwarded_for`: слева то, что
    прислал клиент, справа — дописанный прокси настоящий адрес.
    """
    headers = {'x-forwarded-for': '1.2.3.4, 203.0.113.7'}
    assert resolve_client_ip(headers, NGINX, TRUSTED) == '203.0.113.7'


def test_long_spoofed_chain_still_yields_rightmost():
    headers = {'x-forwarded-for': 'a, b, c, 198.51.100.9'}
    assert resolve_client_ip(headers, NGINX, TRUSTED) == '198.51.100.9'


def test_real_ip_wins_over_forwarded_for():
    headers = {'x-real-ip': '203.0.113.9', 'x-forwarded-for': '1.2.3.4, 203.0.113.7'}
    assert resolve_client_ip(headers, NGINX, TRUSTED) == '203.0.113.9'


def test_falls_back_to_peer_when_no_headers():
    assert resolve_client_ip({}, NGINX, TRUSTED) == NGINX


def test_untrusted_peer_headers_are_ignored_entirely():
    """Прямое обращение к сервису: клиент не должен управлять своим ключом."""
    headers = {'x-real-ip': '1.1.1.1', 'x-forwarded-for': '1.1.1.1'}
    assert resolve_client_ip(headers, '8.8.8.8', TRUSTED) == '8.8.8.8'


def test_returns_none_when_untrusted_and_no_peer():
    assert resolve_client_ip({'x-real-ip': '1.1.1.1'}, None, TRUSTED) is None


def test_blank_real_ip_falls_through_to_forwarded_for():
    headers = {'x-real-ip': '   ', 'x-forwarded-for': '1.2.3.4, 203.0.113.7'}
    assert resolve_client_ip(headers, NGINX, TRUSTED) == '203.0.113.7'


def test_forwarded_for_with_only_separators_falls_back_to_peer():
    assert resolve_client_ip({'x-forwarded-for': ' , , '}, NGINX, TRUSTED) == NGINX


def test_empty_networks_honours_headers_for_in_process_tests():
    headers = {'x-real-ip': '5.5.5.5'}
    assert resolve_client_ip(headers, None, []) == '5.5.5.5'
