"""Optional public Kraken collector; default dry-run, explicit --execute required."""
import argparse
import os
import stat
import fcntl
import urllib.error
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

class CollectorFailure(Exception):
    def __init__(self, code, http_status=None):
        allowed={'PAIR_UNKNOWN','PROVIDER_HTTP_ERROR','PROVIDER_TRANSPORT_ERROR','PROVIDER_INVALID_PAYLOAD','CACHE_INVALID','STORE_WRITE_ERROR'}
        self.code=code if code in allowed else 'PROVIDER_INVALID_PAYLOAD';self.http_status=http_status
        super().__init__(code)


def store_directory(path):
    """Walk from / using pinned no-follow directory fds, including every parent."""
    parts=Path(path).absolute().parts
    if '..' in parts:raise OSError('Invalid store path')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for component in parts[1:]:
            try:child=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(component,0o700,dir_fd=fd)
                child=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                try:
                    os.fchmod(child,0o700)
                    os.fsync(fd)
                except BaseException:
                    os.close(child);raise
            os.close(fd);fd=child
        return fd
    except BaseException:
        os.close(fd);raise


def read_cache(fd):
    os.lseek(fd,0,os.SEEK_SET)
    with os.fdopen(os.dup(fd),'r',encoding='utf-8') as stream:raw=stream.read()
    r.need(not raw or raw.endswith('\n'))
    rows=[]
    for line in raw.splitlines():
        item=json.loads(line,parse_constant=r.reject_constant,object_pairs_hook=r.unique_keys)
        r.need(isinstance(item,dict));r.check_finite(item);r.candle(item);rows.append(item)
    return rows


def append_cache(fd, rows):
    for row in rows:
        view=memoryview((r.canonical(row)+'\n').encode())
        while view:
            count=os.write(fd,view);r.need(count>0);view=view[count:]
    os.fsync(fd)


def query_pair(pair):return r.pair_name(pair).replace('/','')

def fetch(pair):
    # Kraken's public OHLC endpoint rejects the slash form for the benchmark ('XBT/USD' -> EQuery:Unknown asset pair); altnames without the slash work for every pair.
    query=urllib.parse.urlencode({'pair':query_pair(pair),'interval':5})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    req=urllib.request.Request('https://api.kraken.com/0/public/OHLC?'+query,headers={'User-Agent':'Jev-research-candle-collector/1'})
    with opener.open(req,timeout=15) as response:
        if response.status!=200:raise CollectorFailure('PROVIDER_HTTP_ERROR',response.status)
        payload=response.read(4*1024*1024+1);r.need(len(payload)<=4*1024*1024)
        return json.loads(payload,parse_constant=r.reject_constant)


def parse_response(payload,now):
    r.need(isinstance(payload,dict) and isinstance(payload.get('error'),list))
    if payload['error']:
        r.need(all(isinstance(x,str) for x in payload['error']))
        raise CollectorFailure('PAIR_UNKNOWN')
    result=payload.get('result');r.need(isinstance(result,dict))
    keys=[k for k in result if k!='last'];r.need(len(keys)==1)
    raw=result[keys[0]];r.need(isinstance(raw,list) and len(raw)<=721)  # 720 closed candles + the current one, which is dropped below
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
    try:directory=store_directory(cache_dir)
    except OSError:
        report['pair_results']={pair:{'status':'FAILED','code':'STORE_WRITE_ERROR'} for pair in report['pairs']}
        report['status']='BLOCKED';return report
    try:
        for index,pair in enumerate(report['pairs']):
            if index:sleep(1)
            fd=None;stage='STORE_WRITE_ERROR'
            try:
                name=r.candle_path('.',pair).name
                # O_NONBLOCK prevents hanging on an unexpected FIFO; NOFOLLOW
                # protects the file itself, while directory fd pins its parents.
                try:fd=os.open(name,os.O_RDWR|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=directory)
                except OSError as error:
                    if error.errno==__import__('errno').ELOOP:raise CollectorFailure('CACHE_INVALID') from None
                    raise
                if not stat.S_ISREG(os.fstat(fd).st_mode):raise CollectorFailure('CACHE_INVALID')
                if os.path.samestat(os.fstat(fd),os.stat(log)):raise CollectorFailure('CACHE_INVALID')
                fcntl.flock(fd,fcntl.LOCK_EX)
                os.fchmod(fd,0o600)
                stage='CACHE_INVALID';existing=read_cache(fd)
                stage='PROVIDER_INVALID_PAYLOAD';report['requests']+=1
                try:payload=fetcher(pair)
                except urllib.error.HTTPError as error:raise CollectorFailure('PROVIDER_HTTP_ERROR',error.code) from None
                except (urllib.error.URLError,TimeoutError,ConnectionError):raise CollectorFailure('PROVIDER_TRANSPORT_ERROR') from None
                rows=parse_response(payload,now if fixed_clock else time.time())
                known={r.canonical(x) for x in existing};new=[]
                for row in rows:
                    if r.canonical(row) not in known:new.append(row);known.add(r.canonical(row))
                stage='STORE_WRITE_ERROR';append_cache(fd,new);os.fsync(directory)
                report['stored']+=len(new);report['pair_results'][pair]={'status':'OK','stored':len(new)}
            except Exception as error:
                code=error.code if isinstance(error,CollectorFailure) else stage
                failure={'status':'FAILED','code':code}
                if isinstance(error,CollectorFailure) and type(error.http_status) is int and 100<=error.http_status<=599:
                    failure['http_status']=error.http_status
                report['pair_results'][pair]=failure
            finally:
                if fd is not None:os.close(fd)
    finally:os.close(directory)
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
