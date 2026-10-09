import copy
import json
import math
from pathlib import Path
import pytest
from scripts import research_core as r
from scripts import settle_decisions as settle, evaluate_decisions as evaluate, collect_candles as collect
from app.core.decision_log import prompt_version

ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research_protocol.json'

def record(i=1,t=1800100,symbol='ETH',action='BUY'):
    return {'schema_version':1,'record_id':f'{i:032x}','ts_utc':t,'prompt_version':prompt_version(),'status':'success',
            'symbol':symbol,'pair':symbol+'/USD','sample_size':50,'sample_count':50,'sample_hash':'a'*64,
            'decision':{'action':action,'trade_levels':{'stop_loss':90 if action=='BUY' else 110,'target_1':110 if action=='BUY' else 90,'target_2':120 if action=='BUY' else 80}}}

def row(t,price=100):return {'open_ts':t,'o':price,'h':price+1,'l':price-1,'c':price,'v':1}
def write(path,rows):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(''.join(r.canonical(x)+'\n' for x in rows))

def cache_fixture(tmp_path,rec,horizon='H72'):
    h=86400 if horizon=='H24' else r.H72;offsets=[0] if horizon=='H24' else [0,-86400,172800]
    indices=sorted({ts for delta in offsets for ts in r.required(rec['ts_utc']+delta,h)})
    cache={pair:({ts:row(ts) for ts in indices},set()) for pair in {r.pair_name(rec['pair']),'XBT/USD'}}
    for pair,(data,_) in cache.items():write(r.candle_path(tmp_path,pair),data.values())
    return cache

@pytest.mark.parametrize('df,p,expected',[(1,.975,12.706),(2,.975,4.303),(5,.975,2.571),(10,.975,2.228),(29,.975,2.045),(60,.975,2.000),(5,.95,2.015),(10,.95,1.812),(30,.95,1.697),(10,.8,.879),(30,.8,.854)])
def test_t_against_independent_purdue_values(df,p,expected):
    assert abs(r.t_quantile(p,df)-expected)<.001

def test_cr1_manual():
    s=r.summary([1.,2.,3.,4.],[0,0,1,1])
    # mean2.5, block residual sums -2,+2; variance2*(4+4)/16=1.
    assert s['mean']==2.5 and s['se']==1
    assert s['ci95'][1]==pytest.approx(2.5+12.706204736,abs=1e-8)
    assert s['mde_approx']>0

def test_protocol_prompt_hash():
    p,pid=r.protocol(PROTOCOL)
    assert p['prompt_version']==prompt_version() and r.sha(pid)

@pytest.mark.parametrize('t,first,last',[(100,0,86100),(300,300,86400)])
def test_exact_boundaries(t,first,last,tmp_path):
    rec=record(t=t);cache=cache_fixture(tmp_path,rec,'H24')
    result=r.settle_record(rec,'H24',cache,'a'*64,t+86400)
    assert result['settle_status']=='OK'
    assert result['coverage']['ETH/USD']['present'][0]==first
    assert result['coverage']['ETH/USD']['present'][-1]==last
    assert result['windows']['main']['entry_ts']==first+300

def test_entry_candle_excluded_and_sl_tie(tmp_path):
    rec=record();cache=cache_fixture(tmp_path,rec)
    indices=r.required(rec['ts_utc'],r.H72);data=cache['ETH/USD'][0]
    data[indices[0]].update(h=200,l=1)
    result=r.settle_record(rec,'H72',cache,'a'*64,rec['ts_utc']+120*3600)
    assert result['windows']['main']['first_hit']=='NONE'
    data[indices[1]].update(h=121,l=89)
    result=r.settle_record(rec,'H72',cache,'a'*64,rec['ts_utc']+120*3600)
    assert result['windows']['main']['first_hit']=='SL'
    assert result['windows']['main']['hit_ts']['TP1'] is None

def test_hand_computed_buy_sell_btc_and_determinism(tmp_path):
    rec=record();cache=cache_fixture(tmp_path,rec);indices=r.required(rec['ts_utc'],r.H72)
    cache['ETH/USD'][0][indices[-1]].update(c=110,h=111)
    cache['XBT/USD'][0][indices[-1]].update(c=105,h=106)
    buy=r.settle_record(rec,'H72',cache,'a'*64,rec['ts_utc']+432000)
    assert buy['windows']['main']['ret']==pytest.approx(.1)
    assert buy['windows']['main']['alpha']==pytest.approx(.05)
    assert buy['windows']['main']['fade']==pytest.approx(-.05)
    assert r.canonical(buy)==r.canonical(r.settle_record(rec,'H72',cache,'a'*64,rec['ts_utc']+999999))
    rec['decision']['action']='SELL'
    sell=r.settle_record(rec,'H72',cache,'a'*64,rec['ts_utc']+432000)
    assert sell['windows']['main']['ret']==pytest.approx(-.1) and sell['windows']['main']['alpha']==pytest.approx(-.05)
    btc=record(symbol='BTC');result=r.settle_record(btc,'H72',cache,'a'*64,btc['ts_utc']+432000)
    assert result['benchmark_self'] is True

def test_future_gaps_incomplete_then_ok_and_conflicts(tmp_path):
    rec=record();log=tmp_path/'log';out=tmp_path/'settled';audit=tmp_path/'audit';cache_dir=tmp_path/'cache'
    write(log,[rec]);cache_fixture(cache_dir,rec)
    result=settle.settle(log,cache_dir,out,audit,PROTOCOL,now=rec['ts_utc']+72*3600)
    assert result['incomplete']==1 and not r.jsonl(out)
    assert settle.settle(log,cache_dir,out,audit,PROTOCOL,'H24',rec['ts_utc']+86400)['ok_new']==1
    result=settle.settle(log,cache_dir,out,audit,PROTOCOL,now=rec['ts_utc']+432000)
    assert result['ok_new']==1
    before=out.read_bytes()
    assert settle.settle(log,cache_dir,out,audit,PROTOCOL,now=rec['ts_utc']+432000)['ok_new']==0
    assert out.read_bytes()==before
    rows=r.jsonl(out);rows[-1]['candles_hash']='f'*64;r.append_jsonl(out,[rows[-1]])
    result=evaluate.evaluate(log,out,cache_dir,PROTOCOL,rec['ts_utc']+432000)
    assert result['conflicts']['settlements']==1 and result['n']==0
    assert result['verdict']=='SAMPLE_TOO_SMALL'

def cohort(mean=.02):
    result=[]
    for i in range(40):
        t=1000000+i*86400;noise=((i//3)%3-1)*.0001
        result.append({'record_id':f'{i:032x}','symbol':('ETH','SOL','ADA')[i%3], 'ts_utc':t,'entry_ts':t+200,
                       'alpha':mean+noise,'ret':mean+noise+.001,'fade':-mean-noise,
                       'minus24':noise,'plus48':-noise,'first_hit':'NONE','side':'BUY' if i%2 else 'SELL'})
    return result

@pytest.mark.parametrize('mean,verdict',[(.02,'EDGE_POSITIVE'),(0.,'NO_EDGE'),(.004,'INCONCLUSIVE')])
def test_verdict_fixtures(mean,verdict):
    rows=cohort(mean);result=r.evaluate_rows(rows,1000000,1000000+40*86400,r.PARAMS)
    assert result['verdict']==verdict and result['greedy_n']>=15
    assert result['statistics']['alpha']['mde_approx']>0 and result['fade_mirror']
    assert 0<result['permutation_p']<=1

def test_sample_small_preview_and_degenerate():
    rows=cohort();result=r.evaluate_rows(rows[:10],1000000,1000000+40*86400,r.PARAMS)
    assert result['verdict']=='SAMPLE_TOO_SMALL' and 'statistics' not in result
    assert 'statistics' in r.evaluate_rows(rows[:10],1000000,1000000+40*86400,r.PARAMS,True)
    for x in rows:x.update(alpha=.02,ret=.021,fade=-.02)
    assert r.evaluate_rows(rows,1000000,1000000+40*86400,r.PARAMS)['verdict']=='DEGENERATE'

def test_greedy_gate_and_boundary_counterexample():
    rows=cohort();a=copy.deepcopy(rows[0]);b=copy.deepcopy(rows[1]);a.update(symbol='ETH',entry_ts=71*3600);b.update(symbol='ETH',entry_ts=73*3600)
    assert len(r.greedy([a,b]))==1
    for row in rows:row['entry_ts']=1000000  # Main blocks ample, greedy sample only3.
    assert r.evaluate_rows(rows,1000000,1000000+40*86400,r.PARAMS)['verdict']=='SAMPLE_TOO_SMALL'

def test_collect_closed_only_dryrun_and_conflict(tmp_path):
    rec=record();log=tmp_path/'log';write(log,[rec])
    calls=[];sleeps=[]
    def fetch(pair):
        calls.append(pair)
        return {'error':[],'result':{'PAIR':[[1800000,'100','101','99','100','100','2',1],[1800300,'100','101','99','100','100','2',1]],'last':1}}
    assert collect.collect(log,tmp_path/'cache',1800400,False,fetch,sleeps.append)['requests']==0
    result=collect.collect(log,tmp_path/'cache',1800400,True,fetch,sleeps.append)
    assert result['requests']==2 and result['stored']==2 and sleeps==[1]
    assert len(r.candles(tmp_path/'cache','ETH/USD')[0])==1
    payload=fetch('ETH/USD');payload['result']['PAIR'][0][0]=1800600
    assert collect.parse_response(payload,1800400)==[]

@pytest.mark.parametrize('damage',['truncated','nan','bad_action','pair','bool_ts','negative_count'])
def test_malformed_records_fail_closed(tmp_path,damage):
    rec=record();path=tmp_path/'log'
    if damage=='bad_action':rec['decision']['action']='UNKNOWN'
    if damage=='pair':rec['pair']='../../private/USD'
    if damage=='bool_ts':rec['ts_utc']=True
    if damage=='negative_count':rec['sample_count']=-1
    if damage=='nan':path.write_text('{"ts_utc":NaN}\n')
    else:
        write(path,[rec])
        if damage=='truncated':path.write_text(path.read_text().rstrip())
    with pytest.raises(r.InvalidData):r.unique_records(path)

def test_conflicting_records_and_candles(tmp_path):
    rec=record();other=copy.deepcopy(rec);other['ts_utc']+=1;path=tmp_path/'log';write(path,[rec,rec,other])
    assert r.unique_records(path)==({},1)
    a=row(1800000);b=dict(a,c=100.5);write(r.candle_path(tmp_path,'ETH/USD'),[a,a,b])
    data,bad=r.candles(tmp_path,'ETH/USD');assert not data and bad=={1800000}
    badrow=dict(a,h=98)
    with pytest.raises(r.InvalidData):r.candle(badrow)

def test_protocol_join_tampering_and_output_safety(tmp_path):
    rec=record();log=tmp_path/'log';write(log,[rec]);cache_fixture(tmp_path/'cache',rec)
    with pytest.raises(r.InvalidData):settle.settle(log,tmp_path/'cache',log,tmp_path/'audit',PROTOCOL,now=rec['ts_utc']+432000)
    out=tmp_path/'out';settle.settle(log,tmp_path/'cache',out,tmp_path/'audit',PROTOCOL,now=rec['ts_utc']+432000)
    rows=r.jsonl(out);rows[0]['windows']['main']['alpha']=100;write(out,rows)
    result=evaluate.evaluate(log,out,tmp_path/'cache',PROTOCOL,rec['ts_utc']+40*86400)
    assert result['n']==0 and result['conflicts']['settlements']==1


def test_cli_dryrun_preview_and_strict_json(tmp_path,capsys,monkeypatch):
    rec=record();log=tmp_path/'log';write(log,[rec])
    monkeypatch.setattr(collect,'fetch',lambda *a:pytest.fail('NETWORK'))
    assert collect.main(['--log',str(log),'--candles',str(tmp_path/'cache')])==0
    assert 'DRY_RUN' in capsys.readouterr().out
    args=['--log',str(log),'--settlements',str(tmp_path/'empty'),'--candles',str(tmp_path/'cache'),'--preview']
    assert evaluate.main(args)==0
    assert 'PREVIEW — NIE JEST WERDYKTEM' in capsys.readouterr().out
    log.write_text('{"x":1,"x":2}\n')
    with pytest.raises(r.InvalidData):r.jsonl(log)
    log.write_text('{"huge":'+('9'*500)+'}\n')
    with pytest.raises(r.InvalidData):r.jsonl(log)


def test_missing_candle_and_corruption_are_not_replaced(tmp_path):
    rec=record();cache=cache_fixture(tmp_path,rec);index=r.required(rec['ts_utc'],r.H72)[10]
    del cache['ETH/USD'][0][index]
    result=r.settle_record(rec,'H72',cache,'a'*64,rec['ts_utc']+432000)
    assert result['settle_status']=='INCOMPLETE' and index in result['coverage']['ETH/USD']['missing']


def test_nonoverlap_noedge_and_placebo_are_required():
    rows=cohort(0)
    # Main is near zero but greedy has a positive bias -> must not claim NO_EDGE.
    # Separate direct check verifies symmetric TOST with a handcrafted sample.
    for i,x in enumerate(rows):
        x['entry_ts']=1000000+(i//2)*72*3600
    selected=r.greedy(rows)
    assert all(b['entry_ts']>=a['entry_ts']+r.H72 for sym in ('ETH','SOL','ADA') for a,b in zip([x for x in selected if x['symbol']==sym],[x for x in selected if x['symbol']==sym][1:]))
    rows=cohort(.02)
    for x in rows:x['plus48']+=.01
    assert r.evaluate_rows(rows,1000000,1000000+40*86400,r.PARAMS)['verdict']=='INCONCLUSIVE'


def test_no_edge_requires_greedy_equivalence():
    base=cohort(0);rows=[]
    for i in range(80):
        item=copy.deepcopy(base[i%40]);t=1000000+i*43200
        item.update(record_id=f'{i:032x}',ts_utc=t,entry_ts=t+200,symbol=('ETH','SOL')[i%2]);rows.append(item)
    selected={x['record_id'] for x in r.greedy(rows)}
    negative=-.004*len(selected)/(80-len(selected))
    for i,x in enumerate(rows):
        noise=((i//6)%3-1)*.00003
        alpha=(.004 if x['record_id'] in selected else negative)+noise
        x.update(alpha=alpha,ret=alpha+.001,fade=-alpha,minus24=noise,plus48=-noise)
    result=r.evaluate_rows(rows,1000000,1000000+41*86400,r.PARAMS)
    assert r.equivalent(result['statistics']['alpha'],.003)
    assert not r.equivalent(result['greedy'],.003)
    assert result['verdict']=='INCONCLUSIVE'


def test_collector_final_catchup_after_horizon(tmp_path):
    rec=record();log=tmp_path/'log';write(log,[rec])
    for elapsed in (96*3600,120*3600+60):
        report=collect.collect(log,tmp_path/'cache',now=rec['ts_utc']+elapsed)
        assert report['pairs']==['ETH/USD','XBT/USD']
        assert report['requests']==0
    expired=collect.collect(log,tmp_path/'cache',now=rec['ts_utc']+180*3600+1)
    assert expired['pairs']==[] and expired['coverage_status']=='INCOMPLETE'
    assert expired['past_catchup_records']==1
