import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock
import httpx
import pytest
from app.main import app
from app.core.config import settings
from app.core.freshness import social_is_fresh
from app.api.v1 import analyze as api
from app.services import twitter_service as tw
from test_dedup import provider, tweet
from test_twitter_retry import transport, page
from test_pipeline import market, social


@pytest.mark.asyncio
@pytest.mark.parametrize('token,client,headers,allowed', [
    ('', '127.0.0.1', {}, True), ('', '::1', {}, True), ('', '::1', {'Host':'[::1]:8788'}, True),
    ('', 'testclient', {}, False), ('', 'localhost', {}, False),
    ('', '198.51.100.1', {}, False),
    ('', '127.0.0.1', {'X-Forwarded-For':'127.0.0.1'}, False),
    ('', '127.0.0.1', {'Forwarded':'for=127.0.0.1'}, False),
    ('', '127.0.0.1', {'Host':'evil.example'}, False),
    ('', '127.0.0.1', {'Origin':'https://evil.example'}, False),
    ('synthetic', '127.0.0.1', {}, False),
    ('synthetic', '127.0.0.1', {'X-Admin-Token':'wrong'}, False),
    ('synthetic', '127.0.0.1', {'X-Admin-Token':'synthetic'}, True),
    ('synthetic', '198.51.100.1', {'X-Admin-Token':'synthetic'}, True),
])
async def test_shared_access_precedes_providers(monkeypatch,token,client,headers,allowed):
    monkeypatch.setattr(settings,'ADMIN_TOKEN',token)
    market_mock=AsyncMock(return_value={'status':'unavailable'})
    social_mock=AsyncMock(return_value={'tweets':[]})
    monkeypatch.setattr(api.market_service,'get_market_data',market_mock)
    monkeypatch.setattr(api.twitter_service,'fetch_tweets',social_mock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=(client,1)),base_url='http://127.0.0.1') as c:
        for method,path,body in [('POST','/api/v1/analyze',{'symbol':'BTC','sample_size':50}),('GET','/api/v1/market/BTC',None),('POST','/api/v1/settings',{}),('GET','/api/v1/settings',None)]:
            res=await c.request(method,path,json=body,headers=headers)
            assert res.status_code==(200 if allowed else 403)
    assert market_mock.call_count==(2 if allowed else 0)
    assert social_mock.call_count==(1 if allowed else 0)


@pytest.mark.asyncio
async def test_neutral_http_errors_and_logs(monkeypatch,caplog):
    marker='SYNTHETIC_SECRET_SENTINEL'
    monkeypatch.setattr(api.market_service,'get_market_data',AsyncMock(side_effect=RuntimeError(marker)))
    monkeypatch.setattr(api.twitter_service,'fetch_tweets',AsyncMock(return_value={'tweets':[]}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as c:
        for method,path,body in [('POST','/api/v1/analyze',{'symbol':'BTC','sample_size':50}),('GET','/api/v1/market/BTC',None)]:
            res=await c.request(method,path,json=body)
            assert res.status_code==500 and marker not in res.text
    assert marker not in caplog.text


@pytest.mark.parametrize('value',[None,'','bad','2026-10-08T01:00:00','0001-01-01T00:00:00+01:00',123])
def test_unknown_dates_stay_unknown(value):
    assert tw.parse_twitter_timestamp(value) is None


@pytest.mark.asyncio
async def test_window_filters_and_valid_pages(provider):
    now=time.time()
    def dated(i,stamp):
        return dict(tweet(i),createdAt=stamp)
    iso=lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
    provider.pages=[{'tweets':[dated(1,''),dated(2,'bad'),dated(3,iso(now-86401)),dated(4,iso(now+60))], 'has_next_page':True,'next_cursor':'next'},
                    {'tweets':[dated(5,iso(now-5))], 'has_next_page':False}]
    result=await tw.TwitterService('synthetic').fetch_tweets('BTC',1)
    assert result['status']=='ok' and result['tweets'][0]['id']=='5'
    assert result['rejected_dates']=={'unknown_date':2,'outside_window':1,'future_date':1}
    assert 'since_time:' in provider.calls[0]['query']
    assert result['valid_until'] <= result['tweets'][0]['timestamp_epoch']+86400


@pytest.mark.asyncio
async def test_publication_expires_cache_before_ttl(provider,monkeypatch):
    now=int(time.time());clock=[now]
    monkeypatch.setattr(tw.time,'time',lambda:clock[0])
    row=dict(tweet(1),createdAt=datetime.fromtimestamp(now-86400+2,timezone.utc).isoformat())
    provider.pages=[{'tweets':[row],'has_next_page':False},{'tweets':[],'has_next_page':False}]
    service=tw.TwitterService('synthetic')
    first=await service.fetch_tweets('BTC',1)
    assert first['status']=='ok' and first['valid_until']==now+2
    clock[0]+=3
    second=await service.fetch_tweets('BTC',1)
    assert second['status']=='unavailable' and len(provider.calls)==2


@pytest.mark.asyncio
async def test_publication_expires_during_model(monkeypatch):
    now=time.time();clock=[now];payload=social()
    payload['tweets'][0]['timestamp_epoch']=now-86400+1
    monkeypatch.setattr(tw.time,'time',lambda:clock[0])
    monkeypatch.setattr(api.market_service,'get_market_data',AsyncMock(return_value=market()))
    monkeypatch.setattr(api.twitter_service,'fetch_tweets',AsyncMock(return_value=payload))
    async def model(**kw):
        clock[0]+=2
        return {'action':'BUY'}
    monkeypatch.setattr(api.typesafe_service,'evaluate_decision',model)
    result=await api.analyze_asset(api.AnalyzeRequest(symbol='BTC',sample_size=50))
    assert result['decision'] is None


@pytest.mark.asyncio
async def test_fourth_attempt_success(transport):
    transport['replies']=[(429,{}, {})]*3+[(200,page(0,50),{})]
    result=await tw.TwitterService('synthetic').fetch_tweets('BTC',50)
    assert result['status']=='ok' and len(transport['calls'])==4


@pytest.mark.asyncio
async def test_sequential_cooldown_retained(transport):
    service=tw.TwitterService('synthetic')
    transport['replies']=[(429,{}, {'Retry-After':'120'})]
    first=await service.fetch_tweets('BTC',50)
    assert first['reason']=='rate_limit_timeout'
    transport['replies']=[(200,page(0,50),{})]
    second=await service.fetch_tweets('BTC',50)
    assert second['reason']=='rate_limit_timeout' and len(transport['calls'])==1
    transport['clock'][0]=119
    third=await service.fetch_tweets('BTC',50)
    assert third['status']=='ok' and transport['calls'][-1][0]>=120


def test_capacity_does_not_evict_cooldowns(monkeypatch):
    monkeypatch.setattr(tw,'MAX_GATES',2)
    for key in ('a','b'):
        gate=tw.request_gate(key);gate.not_before=tw._monotonic()+500
    assert tw.request_gate('c') is None
    assert set(tw._request_gates)=={'a','b'}


@pytest.mark.parametrize('day',range(7))
@pytest.mark.parametrize('style',['twitter','gmt','iso','offset'])
def test_all_weekday_publication_formats(day,style):
    from datetime import timedelta
    dt=datetime(2026,10,5,12,34,56,tzinfo=timezone.utc)+timedelta(days=day)
    value={'twitter':dt.strftime('%a %b %d %H:%M:%S +0000 %Y'),
           'gmt':dt.strftime('%a, %d %b %Y %H:%M:%S GMT'),
           'iso':dt.isoformat(), 'offset':dt.astimezone(timezone(timedelta(hours=2))).isoformat()}[style]
    assert tw.parse_twitter_timestamp(value)==int(dt.timestamp())


@pytest.mark.asyncio
async def test_rfc_thursday_reaches_real_ingestion(provider,monkeypatch):
    fixed=datetime(2026,10,8,12,tzinfo=timezone.utc)
    monkeypatch.setattr(tw.time,'time',lambda:fixed.timestamp())
    provider.pages=[{'tweets':[dict(tweet(1),createdAt='Thu Oct 08 11:59:00 +0000 2026')],'has_next_page':False}]
    result=await tw.TwitterService('synthetic').fetch_tweets('BTC',1)
    assert result['status']=='ok' and result['count']==1
    assert result['tweets'][0]['timestamp_epoch']==int(fixed.timestamp())-60


@pytest.mark.asyncio
async def test_missing_key_before_saturated_gate(monkeypatch):
    monkeypatch.setattr(tw,'MAX_GATES',1)
    gate=tw.request_gate('existing-hash');gate.not_before=tw._monotonic()+500
    result=await tw.TwitterService().fetch_tweets('BTC',50)
    assert result['reason']=='missing_key' and result['api_pages_called']==0
    assert list(tw._request_gates)==['existing-hash']


@pytest.mark.asyncio
async def test_safe_failure_is_correlatable(monkeypatch,caplog):
    import re
    marker='NEVER_OUTPUT_EXCEPTION_PATH_OR_TOKEN'
    monkeypatch.setattr(api.market_service,'get_market_data',AsyncMock(side_effect=ValueError(marker)))
    monkeypatch.setattr(api.twitter_service,'fetch_tweets',AsyncMock(return_value={'tweets':[]}))
    ids=[]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as c:
        for method,path,body in [('POST','/api/v1/analyze',{'symbol':'BTC','sample_size':50}),('GET','/api/v1/market/BTC',None)]:
            res=await c.request(method,path,json=body)
            correlation=res.headers.get('x-correlation-id','')
            assert res.status_code==500 and re.fullmatch('[0-9a-f]{32}',correlation)
            assert correlation in caplog.text and 'category=invalid_data' in caplog.text
            assert marker not in res.text and marker not in caplog.text
            ids.append(correlation)
    assert len(set(ids))==2


@pytest.mark.asyncio
@pytest.mark.parametrize('exception',[httpx.TimeoutException,httpx.ReadTimeout])
async def test_httpx_timeout_category_and_redaction(monkeypatch,caplog,exception):
    marker='SYNTHETIC_TIMEOUT_SECRET'
    monkeypatch.setattr(api.market_service,'get_market_data',AsyncMock(side_effect=exception(marker)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as c:
        response=await c.get('/api/v1/market/BTC')
    assert response.status_code==500
    assert 'category=timeout' in caplog.text
    assert response.headers['X-Correlation-ID'] in caplog.text
    assert marker not in response.text+caplog.text
