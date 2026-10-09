"""Frozen offline research contract. Standard library only; no application imports."""
import hashlib
import json
import math
from pathlib import Path
import random
import re

HOUR=3600
H72=72*HOUR
ACTIONS={'BUY':1,'STRONG_BUY':1,'SELL':-1,'STRONG_SELL':-1,'HOLD':0,'TAKE_PROFIT':0}
PARAMS={'version':'2.1-corrected','costs':[.003,.005],'delta':.003,'min_n':30,'min_days':30,
        'min_symbols':2,'min_blocks':10,'min_greedy':15,'block_seconds':H72,'permutations':1000,'seed':20261008}

class InvalidData(ValueError):pass

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def finite(x):
    try:return type(x) in (int,float) and math.isfinite(x)
    except OverflowError:return False
def need(condition):
    if not condition:raise InvalidData('INVALID_INPUT')
def reject_constant(value):raise InvalidData('NONFINITE_JSON')
def unique_keys(pairs):
    result={}
    for key,value in pairs:
        need(key not in result);result[key]=value
    return result
def load_json(path):
    try:return json.loads(Path(path).read_text(),parse_constant=reject_constant,object_pairs_hook=unique_keys)
    except (ValueError,OSError,UnicodeError):raise InvalidData('INVALID_JSON') from None

def jsonl(path):
    path=Path(path)
    if not path.exists():return []
    try:
        raw=path.read_text()
        need(not raw or raw.endswith('\n'))
        rows=[]
        for line in raw.splitlines():
            need(bool(line.strip()))
            row=json.loads(line,parse_constant=reject_constant,object_pairs_hook=unique_keys);need(isinstance(row,dict));check_finite(row);rows.append(row)
        return rows
    except (ValueError,OSError,UnicodeError):raise InvalidData('INVALID_JSONL') from None

def check_finite(value):
    if isinstance(value,dict):
        for v in value.values():check_finite(v)
    elif isinstance(value,list):
        for v in value:check_finite(v)
    elif type(value) in (int,float):need(finite(value))

def sha(value):return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None

def protocol(path):
    p=load_json(path);need(isinstance(p,dict) and set(p)==set(PARAMS)|{'prompt_version','document_sha256'})
    need(all(p[k]==v and type(p[k]) is type(v) for k,v in PARAMS.items()))
    need(sha(p['prompt_version']) and sha(p['document_sha256']))
    doc=Path(path).parent/'RESEARCH-PROTOCOL.md'
    need(hashlib.sha256(doc.read_bytes()).hexdigest()==p['document_sha256'])
    return p,digest(p)

def pair_name(pair):
    need(isinstance(pair,str) and re.fullmatch('[A-Z0-9]{1,15}/USD',pair) is not None)
    return 'XBT/USD' if pair=='BTC/USD' else pair

def candle_path(root,pair):return Path(root)/(pair_name(pair).replace('/','_')+'_5m.jsonl')

def validate_record(r):
    need(r.get('schema_version')==1 and type(r.get('schema_version')) is int)
    need(isinstance(r.get('record_id'),str) and re.fullmatch('[a-f0-9]{32}',r['record_id']) is not None)
    need(sha(r.get('prompt_version')) and finite(r.get('ts_utc')) and 0<=r['ts_utc']<1e11)
    need(r.get('status') in ('success','degraded','unavailable'))
    need(isinstance(r.get('symbol'),str) and re.fullmatch('[A-Z0-9]{1,15}',r['symbol']) is not None)
    for name in ('sample_size','sample_count'):
        need(type(r.get(name)) is int and 0<=r[name]<=1000)
    need(sha(r.get('sample_hash')))
    d=r.get('decision')
    if r['status']=='success':
        need(isinstance(d,dict) and isinstance(d.get('action'),str) and d['action'] in ACTIONS)
        need(pair_name(r.get('pair')).split('/')[0]==('XBT' if r['symbol']=='BTC' else r['symbol']))
        if ACTIONS[d['action']]:
            levels=d.get('trade_levels');need(isinstance(levels,dict))
            need(all(finite(levels.get(k)) and levels[k]>0 for k in ('stop_loss','target_1','target_2')))
    else:need(d is None)
    return r

def unique_records(path):
    need(Path(path).is_file())
    records={};bad=set()
    for r in jsonl(path):
        validate_record(r);key=r['record_id']
        if key in records and canonical(records[key])!=canonical(r):bad.add(key)
        else:records[key]=r
    return {k:v for k,v in records.items() if k not in bad},len(bad)

def candle(row):
    need(set(row)=={'open_ts','o','h','l','c','v'})
    t=row['open_ts'];need(type(t) is int and 0<=t<1e11 and t%300==0)
    need(all(finite(row[k]) for k in ('o','h','l','c','v')))
    need(0<row['l']<=min(row['o'],row['c'])<=max(row['o'],row['c'])<=row['h'] and row['v']>=0)
    return {'open_ts':t,**{k:float(row[k]) for k in ('o','h','l','c','v')}}

def candles(root,pair):
    rows={};bad=set()
    for item in jsonl(candle_path(root,pair)):
        row=candle(item);key=row['open_ts']
        if key in rows and canonical(rows[key])!=canonical(row):bad.add(key)
        else:rows[key]=row
    return {k:v for k,v in rows.items() if k not in bad},bad

def required(t,h):return list(range(math.floor(t/300)*300,math.floor((t+h)/300)*300,300))

def window_result(record,asset,benchmark,t,h):
    indices=required(t,h);entry=asset[indices[0]]['c'];exit_price=asset[indices[-1]]['c']
    sign=ACTIONS[record['decision']['action']];raw=exit_price/entry-1
    bench=benchmark[indices[-1]]['c']/benchmark[indices[0]]['c']-1
    levels=record['decision']['trade_levels'];hits={'SL':None,'TP1':None,'TP2':None};first='NONE'
    after_entry=indices[1:]  # Entry candle high/low precede the entry close.
    for ts in after_entry:
        row=asset[ts]
        sl=row['l']<=levels['stop_loss'] if sign>0 else row['h']>=levels['stop_loss']
        tp1=row['h']>=levels['target_1'] if sign>0 else row['l']<=levels['target_1']
        tp2=row['h']>=levels['target_2'] if sign>0 else row['l']<=levels['target_2']
        if sl:
            hits['SL']=ts+300
            if first=='NONE':first='SL'
            break  # SL wins same-candle ties, including subsequent TP2.
        if tp1 and hits['TP1'] is None:
            hits['TP1']=ts+300
            if first=='NONE':first='TP1'
        if tp2 and hits['TP1'] is not None and hits['TP2'] is None:hits['TP2']=ts+300
    high=max(asset[k]['h'] for k in after_entry);low=min(asset[k]['l'] for k in after_entry)
    return {'entry_price':entry,'entry_ts':indices[0]+300,'close_H':exit_price,'ret':sign*raw,
            'bench_ret':bench,'alpha':sign*(raw-bench),'fade':-sign*(raw-bench),
            'max_high':high,'min_low':low,'mae_pct':(low/entry-1) if sign>0 else (1-high/entry),
            'mfe_pct':(high/entry-1) if sign>0 else (1-low/entry),'first_hit':first,'hit_ts':hits}

def settle_record(record,horizon,cache,protocol_id,now):
    need(horizon in ('H24','H72'));h=24*HOUR if horizon=='H24' else H72
    offsets={'main':0} if horizon=='H24' else {'main':0,'minus24':-24*HOUR,'plus48':48*HOUR}
    pair=pair_name(record['pair']);pairs=sorted({pair,'XBT/USD'});sets={};coverage={};used=[]
    indices=sorted({ts for delta in offsets.values() for ts in required(record['ts_utc']+delta,h)})
    for name in pairs:
        data,bad=cache[name];sets[name]=data
        present=[ts for ts in indices if ts in data and ts+300<=now]
        missing=[ts for ts in indices if ts not in data or ts+300>now]
        coverage[name]={'present':present,'missing':missing,'conflicting':[ts for ts in indices if ts in bad]}
        used.append([name,[[ts]+[data[ts][k] for k in ('o','h','l','c','v')] for ts in present]])
    base={'record_id':record['record_id'],'record_hash':digest(record),'protocol_id':protocol_id,'horizon':horizon,'coverage':coverage}
    if any(v['missing'] for v in coverage.values()):return dict(base,settle_status='INCOMPLETE')
    windows={name:window_result(record,sets[pair],sets['XBT/USD'],record['ts_utc']+delta,h) for name,delta in offsets.items()}
    return dict(base,settle_status='OK',candles_hash=digest(used),benchmark_self=record['symbol']=='BTC',candles_used=len(indices)*len(pairs),windows=windows)

def settlement_index(path):
    index={};bad=set()
    for row in jsonl(path):
        need(row.get('settle_status')=='OK' and row.get('horizon') in ('H24','H72') and sha(row.get('protocol_id')) and sha(row.get('record_hash')) and sha(row.get('candles_hash')))
        need(isinstance(row.get('record_id'),str) and re.fullmatch('[a-f0-9]{32}',row['record_id']) is not None)
        key=(row['record_id'],row['horizon'],row['protocol_id'])
        if key in index and canonical(index[key])!=canonical(row):bad.add(key)
        else:index[key]=row
    return index,bad

def append_jsonl(path,rows):
    # Validate existing contents before appending; never repair/truncate user files.
    jsonl(path);Path(path).parent.mkdir(parents=True,exist_ok=True)
    import os,fcntl
    fd=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        for row in rows:
            data=(canonical(row)+'\n').encode();view=memoryview(data)
            while view:
                n=os.write(fd,view);need(n>0);view=view[n:]
        os.fsync(fd)
    finally:os.close(fd)

# Regularized incomplete beta, continued fraction (Lentz), then Student t inversion.
def beta_fraction(a,b,x):
    tiny=1e-300;c=1.;d=1-(a+b)*x/(a+1);d=1/max(abs(d),tiny)*(1 if d>=0 else -1);h=d
    for m in range(1,1001):
        for aa in (m*(b-m)*x/((a+2*m-1)*(a+2*m)), -(a+m)*(a+b+m)*x/((a+2*m)*(a+2*m+1))):
            d=1+aa*d;d=d if abs(d)>tiny else tiny
            c=1+aa/c;c=c if abs(c)>tiny else tiny
            d=1/d;delta=d*c;h*=delta
        if abs(delta-1)<3e-14:return h
    raise InvalidData('BETA_CONVERGENCE')

def beta_i(a,b,x):
    if x<=0:return 0.
    if x>=1:return 1.
    factor=math.exp(math.lgamma(a+b)-math.lgamma(a)-math.lgamma(b)+a*math.log(x)+b*math.log1p(-x))
    return factor*beta_fraction(a,b,x)/a if x<(a+1)/(a+b+2) else 1-factor*beta_fraction(b,a,1-x)/b

def t_cdf(x,df):
    need(type(df) is int and df>0)
    p=.5*beta_i(df/2,.5,df/(df+x*x))
    return 1-p if x>=0 else p

def t_quantile(p,df):
    need(0<p<1)
    if p<.5:return -t_quantile(1-p,df)
    low=0.;high=1.
    while t_cdf(high,df)<p:high*=2
    for _ in range(100):
        mid=(low+high)/2
        if t_cdf(mid,df)<p:low=mid
        else:high=mid
        if high-low<1e-10:break
    return (low+high)/2

def summary(values,blocks):
    n=len(values);g=len(set(blocks));mean=math.fsum(values)/n if n else None
    result={'n':n,'g':g,'mean':mean,'se':None,'ci95':None,'ci90':None,'mde_approx':None,'degenerate':False}
    if g<2:return result
    sums=[math.fsum(x-mean for x,b in zip(values,blocks) if b==block) for block in sorted(set(blocks))]
    se=math.sqrt(g/(g-1)*math.fsum(v*v for v in sums)/(n*n))
    if len(set(values))==1:se=0.
    result.update(se=se,ci95=[mean-t_quantile(.975,g-1)*se,mean+t_quantile(.975,g-1)*se],
                  ci90=[mean-t_quantile(.95,g-1)*se,mean+t_quantile(.95,g-1)*se],
                  mde_approx=(t_quantile(.975,g-1)+t_quantile(.80,g-1))*se,degenerate=se==0)
    return result

def greedy(rows):
    last={};selected=[]
    for row in sorted(rows,key=lambda r:(r['entry_ts'],r['record_id'])):
        if row['symbol'] not in last or row['entry_ts']>=last[row['symbol']]+H72:
            selected.append(row);last[row['symbol']]=row['entry_ts']
    return selected

def equivalent(s,delta):return s['ci90'] is not None and -delta<s['ci90'][0] and s['ci90'][1]<delta

def evaluate_rows(rows,t0,now,p,preview=False):
    selected=greedy(rows)
    blocks=lambda rs:[math.floor((r['ts_utc']-t0)/H72) for r in rs]
    counts={'n':len(rows),'days':math.floor((now-t0)/86400),'symbols':sorted({r['symbol'] for r in rows}),
            'blocks':len(set(blocks(rows))),'greedy_n':len(selected),'greedy_blocks':len(set(blocks(selected)))}
    enough=(counts['n']>=p['min_n'] and counts['days']>=p['min_days'] and len(counts['symbols'])>=p['min_symbols'] and counts['blocks']>=p['min_blocks'] and counts['greedy_n']>=p['min_greedy'] and counts['greedy_blocks']>=p['min_blocks'])
    if not enough and not preview:return dict(counts,verdict='SAMPLE_TOO_SMALL')
    stats={key:summary([r[key] for r in rows],blocks(rows)) for key in ('alpha','ret','minus24','plus48')}
    gs=summary([r['alpha'] for r in selected],blocks(selected));main=stats['alpha']
    fade=all(abs(r['fade']+r['alpha'])<1e-9 for r in rows)
    if not enough:verdict='SAMPLE_TOO_SMALL'
    elif any(s['degenerate'] for s in [*stats.values(),gs]):verdict='DEGENERATE'
    elif main['ci95'][0]>max(p['costs']) and gs['ci95'][0]>max(p['costs']) and all(equivalent(stats[k],p['delta']) for k in ('minus24','plus48')) and fade:verdict='EDGE_POSITIVE'
    elif equivalent(main,p['delta']) and equivalent(gs,p['delta']):verdict='NO_EDGE'
    else:verdict='INCONCLUSIVE'
    rng=random.Random(p['seed']);permutations=[];bl=blocks(rows)
    for _ in range(p['permutations']):
        signs={b:rng.choice((-1,1)) for b in set(bl)}
        permutations.append(abs(math.fsum(r['alpha']*signs[b] for r,b in zip(rows,bl))/len(rows)) if rows else 0.)
    result=dict(counts,verdict=verdict,statistics=stats,greedy=gs,fade_mirror=fade,
                placebo_tost={k:equivalent(stats[k],p['delta']) for k in ('minus24','plus48')},
                net={str(c):{'mean':main['mean']-c if rows else None,'ci95':[v-c for v in main['ci95']] if main['ci95'] else None} for c in p['costs']},
                permutation_p=(1+sum(x>=abs(main['mean'] or 0) for x in permutations))/(p['permutations']+1),
                mde_note='Approximation: SE treated as known; no exact power claim',preview=preview)
    result['hit_rate']=sum(r['ret']>0 for r in rows)/len(rows) if rows else None
    result['first_hit']={k:sum(r['first_hit']==k for r in rows) for k in ('SL','TP1','TP2','NONE')}
    result['descriptive_sides']={side:summary([r['alpha'] for r in rows if r['side']==side],[b for r,b in zip(rows,bl) if r['side']==side]) for side in ('BUY','SELL')}
    result['descriptive_symbols']={sym:summary([r['alpha'] for r in rows if r['symbol']==sym],[b for r,b in zip(rows,bl) if r['symbol']==sym]) for sym in counts['symbols']}
    return result
