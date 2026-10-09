"""Regression tests for the two defects found on the first live collection (2026-10-09): pair name form and the 721-row payload."""
import pytest
from scripts import collect_candles as c


def payload(rows, start=1791500100):  # aligned to 300 s
    return {'error': [], 'result': {'XXBTZUSD': [[start + 300 * i, '1', '2', '0.5', '1.5', '1', '3', 8] for i in range(rows)], 'last': start + 300 * (rows - 1)}}


@pytest.mark.parametrize('pair,expected', [('XBT/USD', 'XBTUSD'), ('ETH/USD', 'ETHUSD'), ('BTC/USD', 'XBTUSD'), ('XRP/USD', 'XRPUSD')])
def test_query_pair_uses_kraken_altname_without_slash(pair, expected):
    assert c.query_pair(pair) == expected


def test_parse_accepts_720_closed_plus_current_and_rejects_more():
    now = 1791500100 + 300 * 721 + 1
    assert len(c.parse_response(payload(721), now)) == 720
    assert len(c.parse_response(payload(720), now)) == 719
    with pytest.raises(Exception):
        c.parse_response(payload(722), now)
