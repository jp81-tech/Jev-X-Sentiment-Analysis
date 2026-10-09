import json
from datetime import datetime,timezone
from pathlib import Path
import httpx
import pytest
from app.services import twitter_service as twitter
from app.core import decision_log
from scripts import auto_analyze as auto, sample_yield_report as report
from test_auto_analyze import arguments, execute, Fake, NOW, reservation, write

SECRET='SENSITIVE_BODY_URL_TOKEN_SENTINEL'

@pytest.fixture
def provider(monkeypatch):
    state={'reply':None,'calls':0}
    class Client:
        def __init__(self,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,*args,**kw):
            state['calls']+=1
            if isinstance(state['reply'],Exception):raise state['reply']
            return state['reply']
    monkeypatch.setattr(twitter.httpx,'AsyncClient',Client);return state

@pytest.mark.asyncio
@pytest.mark.parametrize('status,reason',[(402,'payment_required'),(401,'http_error'),(403,'http_error'),(500,'http_error'),(503,'http_error')])
async def test_http_classification_redacted(provider,status,reason,caplog):
    provider['reply']=httpx.Response(status,json={'secret':SECRET},request=httpx.Request('GET','https://example.invalid/'+SECRET))
    result=await twitter.TwitterService('synthetic').fetch_tweets('ETH',50)
    assert result['reason']==reason and result['status']=='unavailable' and result['count']==0 and provider['calls']==1
    assert SECRET not in caplog.text+json.dumps(result)
    assert str(status) in caplog.text and reason in caplog.text

@pytest.mark.asyncio
@pytest.mark.parametrize('payload',[[],{}, {'tweets':{}},{'tweets':[1]}, {'tweets':[],'has_next_page':'false'}])
async def test_malformed_schema(provider,payload):
    provider['reply']=httpx.Response(200,json=payload,request=httpx.Request('GET','https://example.invalid'))
    result=await twitter.TwitterService('synthetic').fetch_tweets('ETH',50)
    assert result['reason']=='malformed_response' and result['status']=='unavailable'

@pytest.mark.asyncio
async def test_malformed_json_redacted(provider,caplog):
    provider['reply']=httpx.Response(200,content=SECRET,request=httpx.Request('GET','https://example.invalid'))
    result=await twitter.TwitterService('synthetic').fetch_tweets('ETH',50)
    assert result['reason']=='malformed_response' and SECRET not in caplog.text

@pytest.mark.asyncio
@pytest.mark.parametrize('error,reason',[(httpx.ConnectError(SECRET),'transport_error'),(httpx.ReadError(SECRET),'transport_error'),(httpx.ReadTimeout(SECRET),'fetch_timeout'),(RuntimeError(SECRET),'provider_error')])
async def test_transport_timeout_unknown(provider,error,reason,caplog):
    provider['reply']=error;result=await twitter.TwitterService('synthetic').fetch_tweets('ETH',50)
    assert result['reason']==reason and result['status']=='unavailable' and provider['calls']==1
    assert SECRET not in caplog.text+json.dumps(result)


def test_reason_sets_and_journal_allowlist():
    assert auto.REASONS==decision_log.REASONS
    for reason in auto.REASONS:
        assert decision_log.record_for({'social':{'reason':reason}},[],50,1,2)['reason']==reason
    assert decision_log.record_for({'social':{'reason':SECRET}},[],50,1,2)['reason'] is None

@pytest.mark.parametrize('reason',['payment_required','provider_error','end_of_results',SECRET,None,[]])
def test_scheduler_reason_allowlist(tmp_path,reason):
    args=arguments(tmp_path,symbols=['ETH']);response={'status':'degraded','decision_logged':True,'record_id':'b'*32,'social':{'reason':reason}}
    (result,code),_=execute(args,Fake(response=response));entries=auto.rows(Path(args.run_log).read_bytes(),'X')
    expected=reason if isinstance(reason,str) and reason in auto.REASONS else None
    assert code==0 and entries[-1]['reason']==expected
    assert result['reason_counts']=={expected or 'unknown':1}
    assert SECRET not in json.dumps(result)+Path(args.run_log).read_text()


def test_legacy_runlog_still_counts(tmp_path):
    args=arguments(tmp_path);rows=[]
    for i in range(16):
        item=reservation(aid=f'{i:032x}');item.pop('reason');rows.append(item)
    write(args.run_log,rows);before=Path(args.run_log).read_bytes()
    (result,code),fake=execute(args)
    assert code==78 and result['code']=='DAILY_BUDGET_EXHAUSTED' and not fake.calls and Path(args.run_log).read_bytes()==before


def technical(i,symbol='ETH',reason='end_of_results',count=19,hour=8,date='2026-10-08'):
    ts=datetime.fromisoformat(date+f'T{hour:02}:30:00+00:00').timestamp()
    return dict(record_id=f'{i:032x}',symbol=symbol,status='degraded',reason=reason,sample_count=count,ts_utc=ts,private=SECRET,text=SECRET,author=SECRET)


def test_report_counts_denominators_dates_privacy(tmp_path,capsys):
    rows=[technical(1),technical(2,count=39),technical(3,reason='payment_required',count=0),technical(4,'SOL','provider_error',29,hour=20),technical(5,'SOL','target_reached',50,hour=20)]
    rows+=[rows[0]];path=tmp_path/'log';write(path,rows)
    result=report.report(path);overall=result['overall']
    assert result['valid_unique_records']==5 and result['duplicates_ignored']==1
    assert overall['end_of_results']=={'numerator':2,'denominator':5,'share':.4}
    assert overall['end_of_results_below_50_samples']=={'n':2,'median':29,'min':19}
    assert overall['payment_required']['share']==.2 and overall['provider_error']['share']==.2
    assert sum(r['records'] for r in overall['distribution'])==5
    assert [r['utc_hour'] for r in result['by_utc_hour']]==[8,20]
    assert result['by_utc_date_hour'][0]['utc_date']=='2026-10-08'
    assert result['by_symbol'][0]['records']==3
    assert report.main(['--log',str(path)])==0
    captured=capsys.readouterr();assert SECRET not in captured.out+captured.err and '2/3' in captured.out


def test_empty_report(tmp_path):
    path=tmp_path/'log';path.write_bytes(b'');result=report.report(path)
    assert result['status']=='EMPTY' and result['valid_unique_records']==0
    assert result['overall']['end_of_results']['share'] is None and result['overall']['end_of_results_below_50_samples']['median'] is None

@pytest.mark.parametrize('change',[{'sample_count':True},{'sample_count':-1},{'ts_utc':float('nan')},{'reason':[]},{'status':SECRET},{'symbol':'bad!'}, {'ts_utc':1e100}])
def test_invalid_report_failclosed(tmp_path,change,capsys):
    row=technical(1);row.update(change);path=tmp_path/'log';write(path,[technical(2),row])
    assert report.main(['--log',str(path)])==78
    output=capsys.readouterr();assert SECRET not in output.out+output.err
    assert json.loads(output.out)['valid_unique_records'] is None


def test_report_conflict_and_truncated(tmp_path,capsys):
    path=tmp_path/'log';write(path,[technical(1),technical(1,count=20)])
    assert report.main(['--log',str(path)])==78
    path.write_bytes(b'{"record_id":');assert report.main(['--log',str(path)])==78
    assert SECRET not in str(capsys.readouterr())


def test_unknown_reason_does_not_escape(tmp_path):
    path=tmp_path/'log';write(path,[technical(1,reason=SECRET)]);result=report.report(path)
    assert result['overall']['reason_counts']=={'unknown':1} and SECRET not in json.dumps(result)

@pytest.mark.asyncio
async def test_partial_page_then_payment_preserves_real_count(monkeypatch,caplog):
    calls=[]
    created=datetime.now(timezone.utc).isoformat()
    class Client:
        def __init__(self,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,url,**kw):
            calls.append(kw)
            body={'tweets':[{'id':str(i),'createdAt':created} for i in range(19)],'has_next_page':True,'next_cursor':'second'} if len(calls)==1 else {'detail':SECRET}
            return httpx.Response(200 if len(calls)==1 else 402,json=body,request=httpx.Request('GET','https://example.invalid/'+SECRET))
    monkeypatch.setattr(twitter.httpx,'AsyncClient',Client)
    result=await twitter.TwitterService('synthetic').fetch_tweets('ETH',50)
    assert result['reason']=='payment_required' and result['count']==19 and result['status']=='partial'
    assert len(calls)==2 and calls[1]['params']['cursor']=='second'
    assert SECRET not in caplog.text+json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('change',[{'author':[]},{'author':False},{'likeCount':True},{'likeCount':-1},{'likeCount':1.5},{'likeCount':'SECRET'}])
async def test_bad_tweet_fields_malformed(provider,change):
    tweet={'id':'1','createdAt':datetime.now(timezone.utc).isoformat(),**change}
    provider['reply']=httpx.Response(200,json={'tweets':[tweet],'has_next_page':False},request=httpx.Request('GET','https://example.invalid'))
    result=await twitter.TwitterService('synthetic').fetch_tweets('ETH',50)
    assert result['reason']=='malformed_response' and result['count']==0


def test_mixed_legacy_results_new_reason_preserve_budget(tmp_path):
    args=arguments(tmp_path,symbols=['ETH']);reserved=reservation();reserved.pop('reason')
    old_result=auto.event('result','ADA',50,NOW,reserved['attempt_id'],'HTTP_ERROR');old_result.pop('reason')
    write(args.run_log,[reserved,old_result]);before=Path(args.run_log).read_bytes()
    (result,code),_=execute(args,Fake(response={'status':'unavailable','decision_logged':True,'record_id':'b'*32,'social':{'reason':'payment_required'}}))
    rows=auto.rows(Path(args.run_log).read_bytes(),'X')
    assert code==0 and Path(args.run_log).read_bytes().startswith(before)
    assert auto.budget_state(rows,NOW)[0]==2 and rows[-1]['reason']=='payment_required'


@pytest.mark.asyncio
@pytest.mark.parametrize('prefix',[0,1])
@pytest.mark.parametrize('bad',[{'text':[]},{'text':{}},{'text':None},{'text':False},
    {'author':{'userName':{}}},{'author':{'userName':[]}},{'author':{'userName':False}},
    {'author':{'username':{'private':SECRET}}},{'author':{'username':False}}])
async def test_malformed_stats_fields_full_analyze(provider,monkeypatch,caplog,bad,prefix):
    import os
    from unittest.mock import AsyncMock,Mock
    from app.api.v1 import analyze as api
    from test_pipeline import market
    created=datetime.now(timezone.utc).isoformat()
    tweets=[{'id':str(i),'createdAt':created,'text':'valid'} for i in range(prefix)]
    tweets += [{'id':str(i+prefix),'createdAt':created,**bad} for i in range(50)]
    provider['reply']=httpx.Response(200,json={'tweets':tweets,'has_next_page':False},request=httpx.Request('GET','https://example.invalid/'+SECRET))
    monkeypatch.setattr(api,'twitter_service',twitter.TwitterService('synthetic'))
    market_data=market();market_data.update(symbol='ETH',pair='ETH/USD')
    monkeypatch.setattr(api.market_service,'get_market_data',AsyncMock(return_value=market_data))
    model=AsyncMock(side_effect=AssertionError('model must not run'))
    monkeypatch.setattr(api.typesafe_service,'evaluate_decision',model)
    journal=Mock(wraps=api.log_response);monkeypatch.setattr(api,'log_response',journal)
    result=await api.analyze_asset(api.AnalyzeRequest(symbol='ETH',sample_size=50))
    assert result['status']==('degraded' if prefix else 'unavailable')
    assert result['social']['reason']=='malformed_response' and result['social']['count']==prefix
    assert result['decision'] is None and model.await_count==0 and journal.call_count==1
    assert result['decision_logged'] is True
    logged=[json.loads(line) for line in Path(os.environ['JEV_DECISION_LOG']).read_text().splitlines()]
    assert len(logged)==1 and logged[0]['reason']=='malformed_response' and logged[0]['sample_count']==prefix
    assert SECRET not in caplog.text+json.dumps(result)+json.dumps(logged)

@pytest.mark.asyncio
@pytest.mark.parametrize('fields',[{}, {'text':''}, {'author':None}, {'author':{'userName':None}},
    {'author':{'username':None}}, {'author':{'userName':'','username':'valid_alias'}}, {'author':{'userName':'valid'}}])
async def test_valid_missing_text_and_username_preserved(provider,fields):
    from app.services.stats_service import stats_service
    created=datetime.now(timezone.utc).isoformat()
    provider['reply']=httpx.Response(200,json={'tweets':[{'id':str(i),'createdAt':created,**fields} for i in range(50)],'has_next_page':False},request=httpx.Request('GET','https://example.invalid'))
    result=await twitter.TwitterService('synthetic').fetch_tweets('ETH',50)
    assert result['status']=='ok' and result['reason']=='target_reached' and result['count']==50
    assert stats_service.process_tweets(result['tweets'])['sample_size']==50
    assert all(isinstance(t['text'],str) and isinstance(t['author_username'],str) for t in result['tweets'])

@pytest.mark.asyncio
@pytest.mark.parametrize('error,reason',[(httpx.DecodingError(SECRET),'malformed_response'),
    (httpx.TooManyRedirects(SECRET),'transport_error'),(httpx.RequestError(SECRET),'transport_error'),
    (httpx.ReadTimeout(SECRET),'fetch_timeout'),(RuntimeError(SECRET),'provider_error')])
async def test_requesterror_taxonomy(provider,caplog,error,reason):
    provider['reply']=error
    result=await twitter.TwitterService('synthetic').fetch_tweets('ETH',50)
    assert result['status']=='unavailable' and result['reason']==reason and provider['calls']==1
    assert SECRET not in caplog.text+json.dumps(result)
