"""Optional public Kraken collector; default dry-run, explicit --execute required."""
import argparse
import json
from pathlib import Path
import sys
import time
import urllib.request
import urllib.parse
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts import research_core as r

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**k):return None

def fetch(pair):
    query=urllib.parse.urlencode({'pair':r.pair_name(pair),'interval':5})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    req=urllib.request.Request('https://api.kraken.com/0/public/OHLC?'+query,headers={'User-Agent':'Jev-research-candle-collector/1'})
    with opener.open(req,timeout=15) as response:
        payload=response.read(4*1024*1024+1);r.need(len(payload)<=4*1024*1024)
        return json.loads(payload,parse_constant=r.reject_constant)


def parse_response(payload,now):
    r.need(isinstance(payload,dict) and payload.get('error')==[])
    result=payload.get('result');r.need(isinstance(result,dict))
    keys=[k for k in result if k!='last'];r.need(len(keys)==1)
    raw=result[keys[0]];r.need(isinstance(raw,list) and len(raw)<=720)
    rows=[]
    for values in raw[:-1]:  # Always exclude Kraken's current candle, independent of last.
        r.need(isinstance(values,list) and len(values)>=8)
        r.need(type(values[0]) is int)
        row={'open_ts':values[0]}
        for key,index in [('o',1),('h',2),('l',3),('c',4),('v',6)]:
            r.need(type(values[index]) in (str,int,float));row[key]=float(values[index])
        r.candle(row)
        if row['open_ts']+300<=now:rows.append(row)
    return rows


def collect(log,cache_dir,now=None,execute=False,fetcher=fetch,sleep=time.sleep,track=''):
    fixed_clock = now is not None
    now=time.time() if now is None else now;r.need(r.finite(now) and now>=0)
    r.need(isinstance(track,str))
    symbols=track.split(',') if track else []
    r.need(all(__import__('re').fullmatch('[A-Z0-9]{1,15}',x) is not None for x in symbols))
    records,conflicts=r.unique_records(log)
    directional=[x for x in records.values() if x['status']=='success' and r.ACTIONS[x['decision']['action']] and x['ts_utc']<=now]
    # Keep a final catch-up opportunity after the horizon, within Kraken retention.
    pairs={r.pair_name(x['pair']) for x in directional if now<=x['ts_utc']+180*r.HOUR}
    expired=sum(now>x['ts_utc']+180*r.HOUR for x in directional)
    pairs.update(r.pair_name(sym+'/USD') for sym in symbols)
    if pairs:pairs.add('XBT/USD')
    report={'mode':'EXECUTE' if execute else 'DRY_RUN','pairs':sorted(pairs,key=lambda x:(x!='XBT/USD',x)),'requests':0,'stored':0,'conflicts_records':conflicts,'past_catchup_records':expired,'coverage_status':'INCOMPLETE' if expired else 'NOT_ASSESSED'}
    if not execute:return report
    report['pair_results']={}
    for index,pair in enumerate(sorted(pairs,key=lambda x:(x!='XBT/USD',x))):
        if index:sleep(1)
        try:
            path=r.candle_path(cache_dir,pair);r.need(path.resolve()!=Path(log).resolve())
            existing=r.jsonl(path)
            for row in existing:r.candle(row)
            report['requests']+=1
            rows=parse_response(fetcher(pair),now if fixed_clock else time.time())
            known={r.canonical(x) for x in existing};new=[]
            for row in rows:
                if r.canonical(row) not in known:new.append(row);known.add(r.canonical(row))
            r.append_jsonl(path,new);report['stored']+=len(new)
            report['pair_results'][pair]={'status':'OK','stored':len(new)}
        except Exception:
            report['pair_results'][pair]={'status':'FAILED','code':'INVALID_CACHE_OR_PROVIDER_ERROR'}
    failures=sum(x['status']=='FAILED' for x in report['pair_results'].values())
    report['status']='PARTIAL' if failures and failures<len(pairs) else 'BLOCKED' if failures else 'OK'
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log',required=True);parser.add_argument('--candles',required=True)
    parser.add_argument('--track',default='');parser.add_argument('--execute',action='store_true');parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args(argv)
    try:
        r.need(not(args.execute and args.dry_run))
        result=collect(args.log,args.candles,execute=args.execute,track=args.track)
        print(json.dumps(result,allow_nan=False));return 78 if result.get('status') in ('PARTIAL','BLOCKED') else 0
    except Exception:
        print(json.dumps({'status':'BLOCKED','code':'INVALID_INPUT_OR_PROVIDER_ERROR'}));return 78
if __name__=='__main__':raise SystemExit(main())
