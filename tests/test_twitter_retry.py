import asyncio
from email.utils import formatdate
import httpx
import pytest
from app.services import twitter_service as module


def page(start, count, cursor=None):
    return {'tweets': [{'id': str(i), 'text': 'synthetic', 'createdAt': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()} for i in range(start, start+count)],
            'has_next_page': cursor is not None, 'next_cursor': cursor}


@pytest.fixture
def transport(monkeypatch):
    clock = [0.]
    state = {'replies': [], 'calls': [], 'sleeps': [], 'clock': clock}
    async def sleep(seconds):
        state['sleeps'].append(seconds)
        clock[0] += seconds
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, headers, params):
            state['calls'].append((clock[0], dict(params)))
            status, body, response_headers = state['replies'].pop(0)
            return httpx.Response(status, json=body, headers=response_headers, request=httpx.Request('GET', url))
    monkeypatch.setattr(module.httpx, 'AsyncClient', Client)
    # Aliases are deliberately optional before the fix, allowing the red control.
    monkeypatch.setattr(module, '_monotonic', lambda: clock[0], raising=False)
    monkeypatch.setattr(module, '_sleep', sleep, raising=False)
    return state


@pytest.mark.asyncio
async def test_200_429_same_cursor_then_50(transport):
    transport['replies'] = [(200, page(0,20,'next'), {}), (429, {}, {}),
                            (200, page(15,20,'last'), {}), (200, page(35,15), {})]
    result = await module.TwitterService('synthetic-retry').fetch_tweets('BTC',50)
    assert result['status']=='ok' and result['count']==50
    assert result['api_pages_called']==4 and len({t['id'] for t in result['tweets']})==50
    assert transport['calls'][1][1] == transport['calls'][2][1]
    assert transport['calls'][2][0]-transport['calls'][1][0]>=5
    assert all(t['source']=='twitterapi.io' for t in result['tweets'])


@pytest.mark.asyncio
@pytest.mark.parametrize('header,minimum', [(None,5),('bad',5),('-1',5),('NaN',5),('7',7)])
async def test_retry_after(transport,header,minimum):
    transport['replies']=[(429,{}, {} if header is None else {'Retry-After':header}), (200,page(0,50),{})]
    result=await module.TwitterService('synthetic-header').fetch_tweets('BTC',50)
    assert result['count']==50
    assert transport['calls'][1][0]>=minimum


@pytest.mark.asyncio
async def test_retry_after_http_date(transport):
    transport['replies']=[(429,{}, {'Retry-After':formatdate(module.time.time()+15,usegmt=True)}), (200,page(0,50),{})]
    result=await module.TwitterService('synthetic-date').fetch_tweets('BTC',50)
    assert result['count']==50 and transport['calls'][1][0]>=14


@pytest.mark.asyncio
@pytest.mark.parametrize('initial', [False,True])
async def test_persistent429_bounded(transport,initial):
    transport['replies']=([(200,page(0,20,'next'),{})] if initial else [])+[(429,{}, {})]*20
    result=await module.TwitterService('synthetic-limit').fetch_tweets('BTC',50)
    assert result['reason']=='rate_limited'
    assert result['status']==('partial' if initial else 'unavailable')
    assert result['api_pages_called']<=5
    assert transport['clock'][0]<=75


@pytest.mark.asyncio
async def test_retry_after_beyond_budget_not_retried_early(transport):
    transport['replies']=[(429,{}, {'Retry-After':'120'})]
    result=await module.TwitterService('synthetic-deadline').fetch_tweets('BTC',50)
    assert result['reason']=='rate_limit_timeout' and len(transport['calls'])==1
    assert not transport['sleeps']


@pytest.mark.asyncio
@pytest.mark.parametrize('status',[401,402])
async def test_other4xx_not_retried(transport,status,caplog):
    transport['replies']=[(status,{'error':'DO_NOT_LOG_SECRET'}, {})]
    result=await module.TwitterService('synthetic-no-retry').fetch_tweets('BTC',50)
    assert result['reason']=='provider_error' and len(transport['calls'])==1
    assert transport['sleeps']==[]
    assert 'DO_NOT_LOG_SECRET' not in caplog.text and 'synthetic-no-retry' not in caplog.text


@pytest.mark.asyncio
async def test_total_budget_across_pages(transport):
    transport['replies'] = [reply for i in range(50) for reply in
                            [(429, {}, {'Retry-After':'10'}), (200,page(i,1,str(i+1)),{})]]
    result=await module.TwitterService('synthetic-total').fetch_tweets('BTC',50)
    assert result['status']=='partial' and result['reason']=='rate_limit_timeout'
    assert transport['clock'][0]<=75 and result['count']<50
    assert all(t['source']=='twitterapi.io' for t in result['tweets'])


@pytest.mark.asyncio
async def test_overlapping_fetches_share_cooldown(monkeypatch):
    real_yield=asyncio.sleep
    first_entered, release_first = asyncio.Event(), asyncio.Event()
    clock=[0.]; calls=[]
    async def sleep(seconds):
        clock[0]+=seconds
        await real_yield(0)
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,url,**kwargs):
            ordinal=len(calls);calls.append(clock[0])
            if ordinal==0:
                first_entered.set();await release_first.wait()
                return httpx.Response(429,request=httpx.Request('GET',url))
            return httpx.Response(200,json=page(0,50),request=httpx.Request('GET',url))
    monkeypatch.setattr(module.httpx,'AsyncClient',Client)
    monkeypatch.setattr(module,'_monotonic',lambda:clock[0])
    monkeypatch.setattr(module,'_sleep',sleep)
    first=asyncio.create_task(module.TwitterService('same-synthetic-key').fetch_tweets('BTC',50))
    await first_entered.wait()
    second=asyncio.create_task(module.TwitterService('same-synthetic-key').fetch_tweets('ETH',50))
    await real_yield(0)
    assert len(calls)==1  # Second request cannot bypass the in-flight gate.
    release_first.set()
    results=await asyncio.gather(first,second)
    assert all(r['status']=='ok' and r['count']==50 for r in results)
    assert len(calls)==3 and all(t>=5 for t in calls[1:])


@pytest.mark.asyncio
async def test_partial429_never_calls_model(transport,monkeypatch):
    from app.api.v1 import analyze
    now=module.time.time()
    async def market(symbol):
        return {'symbol':symbol,'source':'kraken','status':'ok','price':1,'rsi_14':50,
                'fetched_at':now,'request_started_at':now,'source_timestamp':None,
                'freshness_basis':'request_start','valid_until':now+120}
    async def forbidden_model(**kwargs):
        pytest.fail('Partial429 sample must not invoke model')
    monkeypatch.setattr(analyze.market_service,'get_market_data',market)
    monkeypatch.setattr(analyze.typesafe_service,'evaluate_decision',forbidden_model)
    monkeypatch.setattr(analyze.twitter_service,'api_key','synthetic-partial')
    transport['replies']=[(200,page(0,20,'next'),{})]+[(429,{},{})]*4
    result=await analyze.analyze_asset(analyze.AnalyzeRequest(symbol='BTC',sample_size=50))
    assert result['status']=='degraded' and result['decision'] is None
    assert result['social']['reason']=='rate_limited' and result['social']['count']==20
