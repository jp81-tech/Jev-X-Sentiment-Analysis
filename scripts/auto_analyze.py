"""Bounded local scheduler client. Dry-run never contacts the backend or writes files."""
import argparse
from collections import Counter
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = {'BUY','STRONG_BUY','HOLD','TAKE_PROFIT','SELL','STRONG_SELL'}
STATUSES = {'success','degraded','unavailable'}
OUTCOMES = {'OK','SKIPPED_GAP','SKIPPED_BUDGET','LOGGING_FAILED','TIMEOUT','HTTP_ERROR'}
FIELDS = {'event','attempt_id','ts_utc','trigger','symbol','sample_size','http_code','elapsed_seconds','status','decision_logged','record_id','action','outcome','correlation_id','log_error_category'}

class Blocked(Exception):
    pass


def number(value):
    try:return type(value) in (int,float) and math.isfinite(value)
    except OverflowError:return False


def matches(value,pattern):return isinstance(value,str) and re.fullmatch(pattern,value) is not None

def require(condition,code):
    if not condition:raise Blocked(code)


def unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate')
        result[key]=value
    return result


def reject(value):raise ValueError('nonfinite')


def decode(data):
    value=json.loads(data,object_pairs_hook=unique,parse_constant=reject)
    def check(v):
        if isinstance(v,dict):
            for x in v.values():check(x)
        elif isinstance(v,list):
            for x in v:check(x)
        elif type(v) in (int,float) and not number(v):raise ValueError('nonfinite')
    check(value);return value


def rows(data,code):
    try:
        require(not data or data.endswith(b'\n'),code)
        return [decode(line) for line in data.splitlines()]
    except (ValueError,TypeError,UnicodeError):raise Blocked(code) from None


def read_fd(fd):
    os.lseek(fd,0,os.SEEK_SET)
    with os.fdopen(os.dup(fd),'rb') as stream:return stream.read()


def open_regular(path,flags,missing=False):
    try:fd=os.open(path,flags|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    except FileNotFoundError:
        if missing:return None
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):os.close(fd);raise Blocked('INVALID_PATH')
    return fd


def journal_state(path,now,run_fd=None):
    fd=open_regular(path,os.O_RDONLY,missing=True)
    if fd is None:return {}
    try:
        if run_fd is not None:
            a,b=os.fstat(fd),os.fstat(run_fd)
            require((a.st_dev,a.st_ino)!=(b.st_dev,b.st_ino),'INVALID_PATH')
        entries=rows(read_fd(fd),'JOURNAL_CORRUPT')
    finally:os.close(fd)
    latest={};ids=set()
    for item in entries:
        require(isinstance(item,dict),'JOURNAL_CORRUPT')
        symbol=item.get('symbol');ts=item.get('ts_utc');rid=item.get('record_id')
        require(matches(symbol,r'[A-Z0-9]{1,15}') and number(ts) and 0<=ts<=now and isinstance(item.get('status'),str) and item.get('status') in STATUSES and matches(rid,r'[0-9a-f]{32}'),'JOURNAL_CORRUPT')
        require(rid not in ids,'JOURNAL_CORRUPT');ids.add(rid)
        latest[symbol]=max(latest.get(symbol,0),ts)
    return latest


def _budget_state(entries,now):
    reservations={};completed=set();latest={}
    for item in entries:
        code='RUN_LOG_CORRUPT'
        require(isinstance(item,dict) and set(item)==FIELDS,code)
        ts=item['ts_utc'];symbol=item['symbol'];event=item['event'];aid=item['attempt_id']
        require(number(ts) and 0<=ts<=now and matches(symbol,r'[A-Z0-9]{1,15}') and type(item['sample_size']) is int and 50<=item['sample_size']<=1000 and item['trigger']=='scheduled',code)
        require(item['http_code'] is None or type(item['http_code']) is int and 100<=item['http_code']<=599,code)
        require(number(item['elapsed_seconds']) and item['elapsed_seconds']>=0,code)
        require(item['status'] is None or item['status'] in STATUSES,code)
        require(item['decision_logged'] is None or type(item['decision_logged']) is bool,code)
        require(item['log_error_category'] is None or item['log_error_category'] in {'permission','storage_full','io','invalid_record'},code)
        require(item['action'] is None or item['action'] in ACTIONS,code)
        for key in ('record_id','correlation_id'):require(item[key] is None or matches(item[key],r'[0-9a-f]{32}'),code)
        if event=='attempt_reserved':
            require(matches(aid,r'[0-9a-f]{32}') and aid not in reservations and item['outcome'] is None,code)
            require(all(item[k] is None for k in ('http_code','status','decision_logged','record_id','action','correlation_id','log_error_category')) and item['elapsed_seconds']==0,code)
            reservations[aid]=item;latest[symbol]=max(latest.get(symbol,0),ts)
        elif event=='result':
            require(matches(aid,r'[0-9a-f]{32}') and aid in reservations and aid not in completed and item['outcome'] in {'OK','LOGGING_FAILED','TIMEOUT','HTTP_ERROR'},code)
            reserved=reservations[aid]
            require(symbol==reserved['symbol'] and item['sample_size']==reserved['sample_size'] and ts>=reserved['ts_utc'],code)
            if item['outcome']=='OK':require(item['http_code']==200 and item['status'] in STATUSES and item['decision_logged'] is True and item['record_id'] is not None,code)
            if item['outcome']=='LOGGING_FAILED':require(item['http_code']==200 and item['status'] in STATUSES and item['decision_logged'] is False,code)
            completed.add(aid)
        elif event=='skipped':
            require(aid is None and item['outcome'] in {'SKIPPED_GAP','SKIPPED_BUDGET'},code)
            require(all(item[k] is None for k in ('http_code','status','decision_logged','record_id','action','correlation_id','log_error_category')) and item['elapsed_seconds']==0,code)
        else:raise Blocked(code)
    return sum(x['ts_utc']>now-86400 for x in reservations.values()),latest


def budget_state(entries,now):
    try:return _budget_state(entries,now)
    except (ValueError,TypeError,KeyError,OverflowError):raise Blocked('RUN_LOG_CORRUPT') from None


def event(kind,symbol,size,now,aid=None,outcome=None):
    return dict(event=kind,attempt_id=aid,ts_utc=now,trigger='scheduled',symbol=symbol,sample_size=size,http_code=None,elapsed_seconds=0,status=None,decision_logged=None,record_id=None,action=None,outcome=outcome,correlation_id=None,log_error_category=None)


def append(fd,item):
    remaining=memoryview((json.dumps(item,allow_nan=False,separators=(',',':'))+'\n').encode())
    while remaining:
        written=os.write(fd,remaining)
        if written<=0:raise OSError('no progress')
        remaining=remaining[written:]
    os.fsync(fd)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**k):return None


def request(port,path,body,token,timeout):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    req=urllib.request.Request(f'http://127.0.0.1:{port}{path}',data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json',**({'X-Admin-Token':token} if token else {})})
    try:
        with opener.open(req,timeout=timeout) as response:
            data=response.read(8*1024*1024+1)
            if len(data)>8*1024*1024:return None,response.status,'INVALID_RESPONSE'
            return decode(data),response.status,None
    except urllib.error.HTTPError as error:return None,error.code,'ACCESS_DENIED' if error.code in (401,403) else 'HTTP_ERROR'
    except TimeoutError:return None,None,'TIMEOUT'
    except urllib.error.URLError as error:return None,None,'TIMEOUT' if isinstance(error.reason,TimeoutError) else 'LOCAL_API_UNAVAILABLE'
    except Exception:return None,None,'INVALID_RESPONSE'


def summarize(mode,symbols,counts,requests,code=None,health='NOT_CHECKED'):
    status='BLOCKED' if code and requests==0 else 'PARTIAL' if code or any(counts.get(k,0) for k in OUTCOMES-{'OK'}) else 'PLANNED' if mode=='DRY_RUN' else 'OK'
    return dict(mode=mode,status=status,symbols=symbols,counts=dict(counts),requests=requests,health=health,**({'code':code} if code else {}))


def run(args,requester=request,clock=time.time,token=''):
    mode='EXECUTE' if args.execute else 'DRY_RUN';counts=Counter();attempts=0;fd=None;health='NOT_CHECKED';logging_failed=False
    try:
        now=args.now if args.now is not None else clock();require(number(now) and now>=0,'INVALID_CLOCK')
        require(Path(args.log).resolve()!=Path(args.run_log).resolve(),'INVALID_PATH')
        if not args.execute:
            latest=journal_state(args.log,now)
            fd=open_regular(args.run_log,os.O_RDONLY,missing=True)
            if fd is None:used,reserved=0,{}
            else:
                # Also rejects hardlinks to the read-only decision journal.
                journal_state(args.log,now,fd)
                used,reserved=budget_state(rows(read_fd(fd),'RUN_LOG_CORRUPT'),now)
            for symbol in args.symbols:
                if now-max(latest.get(symbol,-float('inf')),reserved.get(symbol,-float('inf')))<args.min_gap_hours*3600:counts['SKIPPED_GAP']+=1
                elif used>=args.max_per_day or counts['PLANNED']>=args.max_per_run:counts['SKIPPED_BUDGET']+=1
                else:counts['PLANNED']+=1;used+=1
            return summarize(mode,args.symbols,counts,0),0
        Path(args.run_log).parent.mkdir(parents=True,exist_ok=True)
        fd=open_regular(args.run_log,os.O_RDWR|os.O_CREAT|os.O_APPEND)
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise Blocked('RUN_LOCKED') from None
        latest=journal_state(args.log,now,fd)
        used,reserved=budget_state(rows(read_fd(fd),'RUN_LOG_CORRUPT'),now)
        os.fchmod(fd,0o600);os.fsync(fd)
        parent=os.open(Path(args.run_log).parent,os.O_RDONLY)
        try:os.fsync(parent)
        finally:os.close(parent)
        if used>=args.max_per_day:raise Blocked('DAILY_BUDGET_EXHAUSTED')
        data,http,error=requester(args.port,'/health',None,token,args.timeout)
        if error:raise Blocked(error if error in ('ACCESS_DENIED','TIMEOUT') else 'LOCAL_API_UNAVAILABLE')
        require(http==200 and isinstance(data,dict) and data.get('status')=='healthy','LOCAL_API_UNAVAILABLE')
        require(data.get('typesafe_configured') is True and data.get('twitter_configured') is True,'KEYS_NOT_CONFIGURED');health='HEALTHY'
        for symbol in args.symbols:
            now=clock();require(number(now) and now>=0,'INVALID_CLOCK')
            used,reserved=budget_state(rows(read_fd(fd),'RUN_LOG_CORRUPT'),now)
            # Refresh manual decisions as well as this run's durable reservations.
            latest=journal_state(args.log,now,fd)
            last=max(latest.get(symbol,-float('inf')),reserved.get(symbol,-float('inf')))
            if now-last<args.min_gap_hours*3600:
                append(fd,event('skipped',symbol,args.sample_size,now,outcome='SKIPPED_GAP'));counts['SKIPPED_GAP']+=1;continue
            if used>=args.max_per_day or attempts>=args.max_per_run:
                append(fd,event('skipped',symbol,args.sample_size,now,outcome='SKIPPED_BUDGET'));counts['SKIPPED_BUDGET']+=1;continue
            aid=uuid.uuid4().hex
            append(fd,event('attempt_reserved',symbol,args.sample_size,now,aid))
            attempts+=1;started=time.monotonic()
            try:data,http,error=requester(args.port,'/api/v1/analyze',{'symbol':symbol,'sample_size':args.sample_size},token,args.timeout)
            except Exception:data,http,error=None,None,'LOCAL_API_UNAVAILABLE'
            item=event('result',symbol,args.sample_size,clock(),aid)
            item['elapsed_seconds']=max(0,time.monotonic()-started)
            item['http_code']=http if type(http) is int and 100<=http<=599 else None
            outcome='TIMEOUT' if error=='TIMEOUT' else 'HTTP_ERROR'
            if http==200 and not error and isinstance(data,dict):
                status=data.get('status');logged=data.get('decision_logged');rid=data.get('record_id')
                if isinstance(status,str) and status in STATUSES and type(logged) is bool:
                    item.update(status=status,decision_logged=logged)
                    item['record_id']=rid if matches(rid,r'[0-9a-f]{32}') else None
                    cid=data.get('correlation_id');item['correlation_id']=cid if matches(cid,r'[0-9a-f]{32}') else None
                    decision=data.get('decision');action=decision.get('action') if isinstance(decision,dict) else None
                    item['action']=action if isinstance(action,str) and action in ACTIONS else None
                    outcome='OK' if logged and item['record_id'] else 'LOGGING_FAILED' if not logged else 'HTTP_ERROR'
                    category=data.get('log_error_category')
                    item['log_error_category']=category if isinstance(category,str) and category in {'permission','storage_full','io','invalid_record'} else None
                    if not logged:logging_failed=True
            item['outcome']=outcome
            # Validate completion before persisting; clock rollback cannot reset budget.
            budget_state(rows(read_fd(fd),'RUN_LOG_CORRUPT')+[item],clock())
            append(fd,item);counts[outcome]+=1
        code='LOGGING_FAILED' if logging_failed else None
        return summarize(mode,args.symbols,counts,attempts,code,health),0 if counts['OK']==len(args.symbols) else 1
    except Blocked as error:return summarize(mode,args.symbols,counts,attempts,str(error),health),78 if attempts==0 else 1
    except Exception:return summarize(mode,args.symbols,counts,attempts,'STORAGE_OR_INPUT_ERROR',health),78 if attempts==0 else 1
    finally:
        if fd is not None:os.close(fd)


def main(argv=None,requester=request,clock=time.time):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8787);parser.add_argument('--symbols',default='ETH,SOL,XRP')
    parser.add_argument('--sample-size',type=int,default=50);parser.add_argument('--min-gap-hours',type=float,default=5)
    parser.add_argument('--max-per-run',type=int,default=3);parser.add_argument('--max-per-day',type=int,default=16)
    parser.add_argument('--log',default=str(ROOT/'app/data/decisions.jsonl'));parser.add_argument('--run-log',default=str(ROOT/'app/data/auto_analyze_runs.jsonl'))
    parser.add_argument('--execute',action='store_true');parser.add_argument('--now',type=float);parser.add_argument('--timeout',type=float,default=150)
    group=parser.add_mutually_exclusive_group();group.add_argument('--token-env',action='store_true');group.add_argument('--token-stdin',action='store_true')
    args=parser.parse_args(argv)
    if args.execute and args.now is not None:parser.error('--now is only available in dry-run')
    args.symbols=args.symbols.split(',')
    if not(args.symbols and len(set(args.symbols))==len(args.symbols) and all(matches(s,r'[A-Z0-9]{1,15}') for s in args.symbols) and 1<=args.port<=65535 and 50<=args.sample_size<=1000 and number(args.min_gap_hours) and args.min_gap_hours>600/3600 and args.max_per_run>0 and args.max_per_day>0 and number(args.timeout) and 0<args.timeout<=600):
        print(json.dumps(summarize('EXECUTE' if args.execute else 'DRY_RUN',[],{},0,'INVALID_ARGUMENTS')));return 78
    token=''
    if args.execute:
        token=os.environ.get('ADMIN_TOKEN','') if args.token_env else sys.stdin.readline(4097).rstrip('\r\n') if args.token_stdin else ''
        try:token.encode('latin-1');valid=len(token)<=4096 and '\r' not in token and '\n' not in token
        except UnicodeError:valid=False
        if not valid:print(json.dumps(summarize('EXECUTE',[],{},0,'INVALID_ACCESS_TOKEN')));return 78
    result,exitcode=run(args,requester,clock,token);print(json.dumps(result,allow_nan=False));return exitcode

if __name__=='__main__':raise SystemExit(main())
