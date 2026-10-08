"""Offline-only diagnostic classification and output redaction."""
import json
import pytest
from scripts import diagnose_local as diagnostic

NOW = 2_000_000_000


def valid_payload():
    return {'symbol': 'BTC', 'status': 'success', 'model_status': 'ok',
            'market': {'status': 'ok', 'symbol': 'BTC', 'source': 'kraken', 'price': 50000,
                       'rsi_14': 50, 'fetched_at': NOW, 'request_started_at': NOW,
                       'valid_until': NOW+120, 'source_timestamp': None, 'freshness_basis': 'request_start'},
            'social': {'symbol': 'BTC', 'target_count': 50, 'status': 'ok', 'source': 'twitterapi.io', 'count': 50, 'fetched_at': NOW,
                       'valid_until': NOW+600, 'api_pages_called': 3},
            'social_stats': {'sample_size': 50},
            'tweets_sample': [{'id': str(i), 'source': 'twitterapi.io', 'text': 'DO_NOT_OUTPUT_TWEET'} for i in range(50)],
            'decision': {'symbol': 'BTC', 'action': 'BUY', 'confidence_pct': 51, 'reason': 'DO_NOT_OUTPUT_ADVICE'}}


def test_valid_analysis_and_no_payload_secrets():
    payload = valid_payload()
    payload.update(api_key='DO_NOT_OUTPUT_KEY', url='https://DO_NOT_OUTPUT_URL')
    payload['social']['reason'] = 'DO_NOT_OUTPUT_ERROR'
    result = diagnostic.classify(payload, 'analysis', NOW)
    assert result['status'] == 'LIVE_CHECK_OK' and result['model_available']
    assert result['social']['pages'] == 3
    output = json.dumps(result)
    assert 'DO_NOT_OUTPUT' not in output and 'BUY' not in output


@pytest.mark.parametrize('mutation', ['null', 'stale-market', 'stale-social', 'partial', 'duplicate', 'mock', 'bad-decision'])
def test_http200_is_not_success(mutation):
    payload = valid_payload()
    if mutation == 'null': payload['decision'] = None
    elif mutation == 'stale-market': payload['market']['valid_until'] = NOW
    elif mutation == 'stale-social': payload['social']['valid_until'] = NOW
    elif mutation == 'partial': payload['social_stats']['sample_size'] = 49
    elif mutation == 'duplicate': payload['tweets_sample'][1]['id'] = '0'
    elif mutation == 'mock': payload['decision']['is_mock'] = True
    else: payload['decision']['action'] = ['DO_NOT_OUTPUT']
    assert diagnostic.classify(payload, 'analysis', NOW)['status'] == 'UNKNOWN'


def test_missing_keys_blocks_before_analysis():
    calls = []
    def request(*args):
        calls.append(args)
        return {'status': 'healthy', 'twitter_configured': False, 'typesafe_configured': True}, {'http_code': 200}
    result = diagnostic.probe('analysis', 8787, request)
    assert result['status'] == 'BLOCKED' and result['codes'] == ['TWITTER_KEY_MISSING']
    assert len(calls) == 1


def test_exactly_one_analysis_no_retry():
    calls = []
    def request(port, path, body=None):
        calls.append((path, body))
        if path == '/health':
            return {'status': 'healthy', 'twitter_configured': True, 'typesafe_configured': True}, {'http_code': 200}
        return valid_payload(), {'http_code': 200}
    assert diagnostic.probe('analysis', 8787, request, lambda: NOW)['status'] == 'LIVE_CHECK_OK'
    assert calls == [('/health', None), ('/api/v1/analyze', {'symbol': 'BTC', 'sample_size': 50})]


def test_http_error_contains_no_exception_text(monkeypatch):
    import urllib.error
    class BadOpener:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError('https://PRIVATE_URL', 401, 'PRIVATE_KEY', {}, None)
    monkeypatch.setattr(diagnostic.urllib.request, 'build_opener', lambda *args: BadOpener())
    payload, report = diagnostic.request(8787, '/health')
    assert payload is None and report['http_code'] == 401
    assert 'PRIVATE' not in json.dumps(report)


def test_market_without_keys_and_api_unknown_separate():
    def request(port, path, body=None):
        return ({'status': 'healthy'} if path == '/health' else valid_payload()['market']), {'http_code': 200}
    result = diagnostic.probe('market', 8787, request, lambda: NOW)
    assert result['status'] == 'LIVE_CHECK_OK' and result['model_available'] is False
    unavailable = diagnostic.probe('market', 8787, lambda *a: (None, {'code': 'CONNECTION_ERROR'}))
    assert unavailable['code'] == 'LOCAL_API_UNAVAILABLE'


@pytest.mark.parametrize('section', ['top', 'market', 'social', 'decision'])
@pytest.mark.parametrize('missing', [False, True])
def test_symbol_binding_required(section, missing):
    payload = valid_payload()
    target = payload if section == 'top' else payload[section]
    if missing:
        target.pop('symbol')
    else:
        target['symbol'] = 'ETH_DO_NOT_ECHO'
    result = diagnostic.classify(payload, 'analysis', NOW)
    assert result['status'] == 'UNKNOWN'
    assert 'ETH_DO_NOT_ECHO' not in json.dumps(result)


@pytest.mark.parametrize('value', [None, 49, 100, '50'])
def test_target_count_matches_requested_sample(value):
    payload = valid_payload()
    if value is None:
        payload['social'].pop('target_count')
    else:
        payload['social']['target_count'] = value
    assert diagnostic.classify(payload, 'analysis', NOW)['status'] == 'UNKNOWN'


def test_probe_token_transport_against_asgi(monkeypatch):
    import asyncio
    import io
    import urllib.error
    import httpx
    from app.main import app
    from app.core.config import settings
    from app.api.v1 import analyze as api
    from unittest.mock import AsyncMock
    monkeypatch.setattr(settings,'ADMIN_TOKEN','SYNTHETIC_ACCESS_SECRET')
    provider=AsyncMock(return_value=valid_payload()['market'])
    monkeypatch.setattr(api.market_service,'get_market_data',provider)
    class Reply(io.BytesIO):
        status=200
    class Opener:
        def open(self,req,timeout):
            async def send():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8787') as c:
                    return await c.request(req.get_method(),req.full_url,headers=dict(req.header_items()),content=req.data)
            res=asyncio.run(send())
            if res.status_code>=400:
                raise urllib.error.HTTPError(req.full_url,res.status_code,'SYNTHETIC_ACCESS_SECRET',{},None)
            return Reply(res.content)
    monkeypatch.setattr(diagnostic.urllib.request,'build_opener',lambda *args:Opener())
    for token,status in [(None,'BLOCKED'),('wrong','BLOCKED'),('SYNTHETIC_ACCESS_SECRET','LIVE_CHECK_OK')]:
        result=diagnostic.probe('market',8787,lambda p,path,body=None:diagnostic.request(p,path,body,token=token),lambda:NOW)
        assert result['status']==status
        if status=='BLOCKED':assert result['code']=='ACCESS_DENIED'
        assert 'SYNTHETIC_ACCESS_SECRET' not in json.dumps(result)
    assert provider.call_count==1


@pytest.mark.parametrize('source',['--token-env','--token-stdin'])
@pytest.mark.parametrize('token',['SYNTHETIC_ACCESS_SECRET','café'])
def test_probe_explicit_token_input_not_echoed(monkeypatch,capsys,source,token):
    import io
    monkeypatch.setenv('ADMIN_TOKEN',token)
    monkeypatch.setattr(diagnostic.sys,'stdin',io.StringIO(token+'\n'))
    monkeypatch.setattr(diagnostic.sys,'argv',['diagnose_local.py','market',source])
    calls=[]
    def request(port,path,body=None,token=None):
        calls.append(token)
        return ({'status':'healthy'} if path=='/health' else valid_payload()['market']),{'http_code':200}
    monkeypatch.setattr(diagnostic,'request',request)
    monkeypatch.setattr(diagnostic,'probe',lambda mode,port,requester: (requester(port,'/health'),{'status':'UNKNOWN'})[1])
    assert diagnostic.main()==78 and calls==[token]
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err


def test_launcher_exact_origin_selected_port(monkeypatch):
    import sys
    import uvicorn
    from scripts import launch_local
    from app.core.config import settings
    monkeypatch.setattr(sys,'argv',['launch_local.py','--port','8912'])
    monkeypatch.setattr(settings,'ALLOWED_ORIGINS',settings.ALLOWED_ORIGINS)
    monkeypatch.setattr(settings,'ALLOWED_HOSTS',settings.ALLOWED_HOSTS)
    calls=[]
    def run(app,**kwargs):
        calls.append(kwargs)
        assert settings.ALLOWED_ORIGINS=='http://127.0.0.1:8912,http://localhost:8912'
        assert settings.ALLOWED_HOSTS=='127.0.0.1,localhost'
    monkeypatch.setattr(uvicorn,'run',run)
    assert launch_local.main()==0
    assert calls[0]['host']=='127.0.0.1' and calls[0]['port']==8912 and calls[0]['proxy_headers'] is False


@pytest.mark.parametrize('source',['--token-env','--token-stdin'])
def test_invalid_header_token_rejected_before_any_request(monkeypatch,capsys,source):
    import io
    secret='żółw'
    monkeypatch.setenv('ADMIN_TOKEN',secret)
    monkeypatch.setattr(diagnostic.sys,'stdin',io.StringIO(secret+'\n'))
    monkeypatch.setattr(diagnostic.sys,'argv',['diagnose_local.py','market',source])
    calls=[]
    def forbidden(*args,**kwargs):
        calls.append(1)
        raise AssertionError('No request permitted')
    monkeypatch.setattr(diagnostic,'request',forbidden)
    assert diagnostic.main()==78 and calls==[]
    captured=capsys.readouterr()
    assert json.loads(captured.out)=={'status':'BLOCKED','code':'INVALID_ACCESS_TOKEN'}
    assert secret not in captured.out+captured.err and captured.err==''
