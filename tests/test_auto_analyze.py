import argparse
import json
import multiprocessing
import os
from pathlib import Path
import stat
import urllib.error
import pytest
from scripts import auto_analyze as a

NOW=2000000000
SECRET='DO_NOT_PRINT_TOKEN_EXCEPTION_BODY'

def arguments(tmp_path,**changes):
    values=dict(port=8787,symbols=['ETH','SOL','XRP'],sample_size=50,min_gap_hours=5,max_per_run=3,max_per_day=16,log=str(tmp_path/'decisions'),run_log=str(tmp_path/'runs'),execute=True,now=None,timeout=150)
    values.update(changes);return argparse.Namespace(**values)


def write(path,rows):Path(path).write_text(''.join(json.dumps(x)+'\n' for x in rows))


def reservation(symbol='ADA',t=NOW-100,aid='a'*32):return a.event('attempt_reserved',symbol,50,t,aid)


class Fake:
    def __init__(self,health=None,response=None,error=None,code=200):
        self.calls=[];self.health=health if health is not None else {'status':'healthy','typesafe_configured':True,'twitter_configured':True}
        self.response=response;self.error=error;self.code=code
    def __call__(self,port,path,body,token,timeout):
        self.calls.append((port,path,body,token,timeout))
        if path=='/health':return self.health,200,None
        return self.response if self.response is not None else {'status':'success','decision_logged':True,'record_id':f'{len(self.calls):032x}','decision':{'action':'BUY'},'correlation_id':None,'tweets':[SECRET],'trade_levels':SECRET},self.code,self.error
    @property
    def posts(self):return [c for c in self.calls if c[2] is not None]


def execute(args,fake=None):
    fake=fake or Fake();return a.run(args,fake,lambda:NOW),fake


def test_success_append_and_safe_fields(tmp_path):
    args=arguments(tmp_path);write(args.run_log,[reservation()]);old=Path(args.run_log).read_bytes()
    (result,code),fake=execute(args)
    assert code==0 and result['counts']=={'OK':3} and result['requests']==3
    assert [c[2]['symbol'] for c in fake.posts]==['ETH','SOL','XRP']
    assert Path(args.run_log).read_bytes().startswith(old)
    entries=a.rows(Path(args.run_log).read_bytes(),'X')
    assert len(entries)==7 and all(set(x)==a.FIELDS for x in entries)
    assert a.budget_state(entries,NOW)[0]==4 and SECRET not in Path(args.run_log).read_text()+json.dumps(result)
    assert stat.S_IMODE(Path(args.run_log).stat().st_mode)==0o600

@pytest.mark.parametrize('health,code',[({'status':'broken'},'LOCAL_API_UNAVAILABLE'),({'status':'healthy','typesafe_configured':False,'twitter_configured':True},'KEYS_NOT_CONFIGURED'),({'status':'healthy','typesafe_configured':True,'twitter_configured':1},'KEYS_NOT_CONFIGURED')])
def test_health_blocks(tmp_path,health,code):
    (result,exitcode),fake=execute(arguments(tmp_path),Fake(health=health))
    assert exitcode==78 and result['code']==code and not fake.posts

@pytest.mark.parametrize('code,error',[(401,'ACCESS_DENIED'),(403,'ACCESS_DENIED'),(500,'HTTP_ERROR'),(None,'TIMEOUT')])
def test_health_transport_errors(tmp_path,code,error):
    calls=[]
    def request(*args):calls.append(args);return None,code,error
    result,exitcode=a.run(arguments(tmp_path),request,lambda:NOW)
    assert exitcode==78 and len(calls)==1 and result['requests']==0
    assert result['code']==(error if error in ('ACCESS_DENIED','TIMEOUT') else 'LOCAL_API_UNAVAILABLE')

@pytest.mark.parametrize('code,error,outcome',[(401,'ACCESS_DENIED','HTTP_ERROR'),(500,'HTTP_ERROR','HTTP_ERROR'),(None,'TIMEOUT','TIMEOUT')])
def test_post_error_no_retry(tmp_path,code,error,outcome):
    args=arguments(tmp_path,symbols=['ETH']);(result,exitcode),fake=execute(args,Fake(code=code,error=error))
    assert exitcode==1 and len(fake.posts)==1 and result['counts']=={outcome:1}
    assert a.budget_state(a.rows(Path(args.run_log).read_bytes(),'X'),NOW)[0]==1


def test_logging_failed_is_partial(tmp_path):
    fake=Fake(response={'status':'degraded','decision_logged':False,'log_error_category':SECRET,'correlation_id':'c'*32})
    (result,code),_=execute(arguments(tmp_path,symbols=['ETH']),fake)
    assert code==1 and result['status']=='PARTIAL' and result['code']=='LOGGING_FAILED' and result['counts']=={'LOGGING_FAILED':1}
    assert SECRET not in json.dumps(result)

@pytest.mark.parametrize('age,expected',[(4,0),(6,1)])
def test_gap_gate(tmp_path,age,expected):
    args=arguments(tmp_path,symbols=['ETH']);write(args.log,[{'record_id':'a'*32,'symbol':'ETH','status':'unavailable','ts_utc':NOW-age*3600}])
    (result,code),fake=execute(args)
    assert len(fake.posts)==expected and code==(0 if expected else 1)


def test_orphan_reservation_enforces_gap(tmp_path):
    args=arguments(tmp_path,symbols=['ETH']);write(args.run_log,[reservation(symbol='ETH')])
    (result,code),fake=execute(args)
    assert code==1 and result['counts']=={'SKIPPED_GAP':1} and not fake.posts

@pytest.mark.parametrize('prior,expected,exitcode',[(16,0,78),(15,1,1)])
def test_day_budget_gate(tmp_path,prior,expected,exitcode):
    args=arguments(tmp_path);write(args.run_log,[reservation(aid=f'{i:032x}') for i in range(prior)])
    (result,code),fake=execute(args)
    assert code==exitcode and len(fake.posts)==expected
    if prior==16:assert not fake.calls and result['code']=='DAILY_BUDGET_EXHAUSTED'


def test_per_run_limit_and_skips_free(tmp_path):
    args=arguments(tmp_path,symbols=['ETH','SOL','XRP','ADA','NEAR']);write(args.run_log,[a.event('skipped','DOGE',50,NOW-10,outcome='SKIPPED_GAP') for _ in range(16)])
    (result,code),fake=execute(args)
    assert code==1 and len(fake.posts)==3 and result['counts']=={'OK':3,'SKIPPED_BUDGET':2}
    assert a.budget_state(a.rows(Path(args.run_log).read_bytes(),'X'),NOW)[0]==3


def test_dryrun_no_io_writes_no_token_read(tmp_path,monkeypatch,capsys):
    def no(*a,**k):pytest.fail('side effect')
    monkeypatch.setattr(a.os,'write',no);monkeypatch.setattr(a.sys.stdin,'readline',no)
    args=['--log',str(tmp_path/'journal'),'--run-log',str(tmp_path/'runs'),'--now',str(NOW),'--token-stdin']
    assert a.main(args,requester=no)==0
    output=json.loads(capsys.readouterr().out)
    assert output['requests']==0 and output['health']=='NOT_CHECKED' and output['counts']=={'PLANNED':3}
    assert not list(tmp_path.iterdir())


def test_execute_now_rejected_before_anything(tmp_path):
    with pytest.raises(SystemExit) as error:a.main(['--execute','--now',str(NOW)],requester=lambda *a:pytest.fail('request'))
    assert error.value.code==2

@pytest.mark.parametrize('source',['env','stdin'])
@pytest.mark.parametrize('token',[SECRET,'café','żółw'])
def test_token_contract(tmp_path,monkeypatch,capsys,source,token):
    import io
    fake=Fake();monkeypatch.setenv('ADMIN_TOKEN',token);monkeypatch.setattr(a.sys,'stdin',io.StringIO(token+'\n'))
    args=['--execute','--symbols','ETH','--log',str(tmp_path/'log'),'--run-log',str(tmp_path/'runs'),'--token-'+source]
    code=a.main(args,fake,lambda:NOW);captured=capsys.readouterr()
    if token=='żółw':assert code==78 and not fake.calls and 'INVALID_ACCESS_TOKEN' in captured.out
    else:assert code==0 and all(c[3]==token for c in fake.calls)
    assert token not in captured.out+captured.err

@pytest.mark.parametrize('bad',[b'{"cut":',b'{"ts_utc":NaN}\n',b'null\n'])
def test_corrupt_runlog_blocks_unchanged(tmp_path,bad):
    args=arguments(tmp_path);Path(args.run_log).write_bytes(bad)
    (result,code),fake=execute(args)
    assert code==78 and result['code']=='RUN_LOG_CORRUPT' and not fake.calls and Path(args.run_log).read_bytes()==bad

@pytest.mark.parametrize('mutate',[lambda x:x.update(ts_utc=NOW+1),lambda x:x.update(ts_utc=None),lambda x:x.update(sample_size=True),lambda x:x.update(status=[]),lambda x:x.update(attempt_id=None),lambda x:x.update(event='result',outcome='OK')])
def test_invalid_budget_structure(tmp_path,mutate):
    args=arguments(tmp_path);item=reservation();mutate(item);write(args.run_log,[item]);before=Path(args.run_log).read_bytes()
    (result,code),fake=execute(args)
    assert code==78 and result['code']=='RUN_LOG_CORRUPT' and not fake.calls and Path(args.run_log).read_bytes()==before


def test_duplicate_reservation_result_and_result_binding(tmp_path):
    reserved=reservation();result=a.event('result','ADA',50,NOW,reserved['attempt_id'],'HTTP_ERROR')
    for entries in ([reserved,reserved],[result],[reserved,result,result],[reserved,{**result,'symbol':'ETH'}]):
        with pytest.raises(a.Blocked,match='RUN_LOG_CORRUPT'):a.budget_state(entries,NOW)

@pytest.mark.parametrize('bad',[b'{"cut":',b'null\n',b'{"symbol":"ETH","ts_utc":null}\n'])
def test_corrupt_journal_blocks(tmp_path,bad):
    args=arguments(tmp_path);Path(args.log).write_bytes(bad)
    (result,code),fake=execute(args)
    assert code==78 and result['code']=='JOURNAL_CORRUPT' and not fake.calls and Path(args.log).read_bytes()==bad

@pytest.mark.parametrize('kind',['same','hardlink','symlink','fifo','directory'])
def test_invalid_paths_no_hang(tmp_path,kind):
    args=arguments(tmp_path);Path(args.log).write_text('')
    if kind=='same':args.run_log=args.log
    if kind=='hardlink':os.link(args.log,args.run_log)
    if kind=='symlink':Path(args.run_log).symlink_to(args.log)
    if kind=='fifo':os.mkfifo(args.run_log)
    if kind=='directory':Path(args.run_log).mkdir()
    (result,code),fake=execute(args)
    assert code==78 and not fake.calls


def test_reservation_and_parent_fsync_before_post(tmp_path,monkeypatch):
    args=arguments(tmp_path,symbols=['ETH']);real=a.os.fsync;events=[]
    def fsync(fd):events.append(('sync',stat.S_ISDIR(os.fstat(fd).st_mode)));real(fd)
    monkeypatch.setattr(a.os,'fsync',fsync);fake=Fake()
    def request(*values):
        if values[2] is not None:
            entries=a.rows(Path(args.run_log).read_bytes(),'X')
            assert entries[-1]['event']=='attempt_reserved'
            assert events[-1]==('sync',False) and ('sync',True) in events
        return fake(*values)
    assert a.run(args,request,lambda:NOW)[1]==0

@pytest.mark.parametrize('stage',['parent','reservation'])
def test_fsync_failure_prevents_post(tmp_path,monkeypatch,stage):
    args=arguments(tmp_path);real=a.os.fsync;calls=[]
    def fsync(fd):
        calls.append(1)
        if len(calls)==(2 if stage=='parent' else 3):raise OSError(SECRET)
        real(fd)
    monkeypatch.setattr(a.os,'fsync',fsync)
    (result,code),fake=execute(args)
    assert code==78 and not fake.posts and SECRET not in json.dumps(result)


def child_lock(args,ready,release):
    fake=Fake()
    def request(*values):
        if values[1]=='/health':ready.set();assert release.wait(10)
        return fake(*values)
    assert a.run(args,request,lambda:NOW)[1]==0


def test_two_process_lock_nonblocking(tmp_path):
    args=arguments(tmp_path);ctx=multiprocessing.get_context('fork');ready=ctx.Event();release=ctx.Event();process=ctx.Process(target=child_lock,args=(args,ready,release));process.start()
    try:
        assert ready.wait(5)
        (result,code),fake=execute(args)
        assert code==78 and result['code']=='RUN_LOCKED' and not fake.calls
    finally:release.set();process.join(5)
    assert process.exitcode==0


def test_crash_after_reservation_preserves_budget_and_gap(tmp_path):
    args=arguments(tmp_path,symbols=['ETH']);fake=Fake()
    def crash(*values):
        if values[2] is not None:raise KeyboardInterrupt()
        return fake(*values)
    with pytest.raises(KeyboardInterrupt):a.run(args,crash,lambda:NOW)
    assert a.budget_state(a.rows(Path(args.run_log).read_bytes(),'X'),NOW)[0]==1
    (result,code),again=execute(args)
    assert code==1 and not again.posts and result['counts']=={'SKIPPED_GAP':1}


def test_transport_fixed_url_no_redirect_no_proxy(tmp_path,monkeypatch):
    seen={}
    class Response:
        status=200
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,n):return b'{}'
    class Opener:
        def open(self,req,timeout):seen.update(url=req.full_url,headers=dict(req.header_items()),timeout=timeout);return Response()
    def build(*handlers):
        assert handlers[0].proxies=={} and isinstance(handlers[1],a.NoRedirect)
        return Opener()
    monkeypatch.setattr(a.urllib.request,'build_opener',build)
    assert a.request(8787,'/health',None,None,150)==({},200,None)
    assert seen['url']=='http://127.0.0.1:8787/health' and 'Origin' not in seen['headers'] and 'X-admin-token' not in seen['headers']
    assert a.NoRedirect().redirect_request(None,None,None,None,None,None) is None


@pytest.mark.parametrize('status',['degraded','unavailable'])
def test_no_decision_can_still_log_successfully(tmp_path,status):
    (result,code),fake=execute(arguments(tmp_path,symbols=['ETH']),Fake(response={'status':status,'decision_logged':True,'record_id':'b'*32,'decision':None}))
    assert code==0 and result['counts']=={'OK':1}

@pytest.mark.parametrize('category',['permission','storage_full','io','invalid_record','SECRET'])
def test_logging_error_category_allowlist(tmp_path,category):
    args=arguments(tmp_path,symbols=['ETH'])
    execute(args,Fake(response={'status':'unavailable','decision_logged':False,'log_error_category':category}))
    entries=a.rows(Path(args.run_log).read_bytes(),'X')
    assert entries[-1]['log_error_category']==(None if category=='SECRET' else category)
    assert entries[-1]['outcome']=='LOGGING_FAILED'

@pytest.mark.parametrize('failure_at',[1,2])
def test_write_failure_stops_posts(tmp_path,monkeypatch,failure_at):
    args=arguments(tmp_path);original=a.os.write;calls=[]
    def write(fd,data):
        calls.append(1)
        if len(calls)==failure_at:raise OSError(SECRET)
        return original(fd,data)
    monkeypatch.setattr(a.os,'write',write)
    (result,code),fake=execute(args)
    assert len(fake.posts)==failure_at-1 and code==(78 if failure_at==1 else 1)
    assert SECRET not in json.dumps(result)


def test_short_write_roundtrip(tmp_path,monkeypatch):
    original=a.os.write
    monkeypatch.setattr(a.os,'write',lambda fd,data:original(fd,data[:7]))
    args=arguments(tmp_path,symbols=['ETH']);assert execute(args)[0][1]==0
    assert len(a.rows(Path(args.run_log).read_bytes(),'X'))==2

@pytest.mark.parametrize('kind',['symlink','fifo','directory'])
def test_invalid_journal_paths_no_hang(tmp_path,kind):
    args=arguments(tmp_path)
    if kind=='symlink':Path(args.log).symlink_to(tmp_path/'absent')
    if kind=='fifo':os.mkfifo(args.log)
    if kind=='directory':Path(args.log).mkdir()
    (result,code),fake=execute(args)
    assert code==78 and not fake.calls


def test_day_boundary_results_not_double_counted():
    old=reservation(t=NOW-86400,aid='a'*32)
    current=reservation(t=NOW-86399,aid='b'*32)
    result=a.event('result','ADA',50,NOW,'b'*32,'HTTP_ERROR')
    assert a.budget_state([old,current,result],NOW)[0]==1

@pytest.mark.parametrize('payload',[b'[]',b'{"x":NaN}',b'x'*(8*1024*1024+1)])
def test_transport_malformed_oversize_neutral(monkeypatch,payload):
    class Response:
        status=200
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,n):return payload
    class Opener:
        def open(self,*a,**k):return Response()
    monkeypatch.setattr(a.urllib.request,'build_opener',lambda *a:Opener())
    data,http,error=a.request(8787,'/health',None,None,150)
    if payload==b'[]':assert data==[]
    else:assert data is None and error=='INVALID_RESPONSE'


def test_transport_error_never_echoes_body(monkeypatch):
    import io
    class Opener:
        def open(self,*args,**kwargs):raise urllib.error.HTTPError('http://secret',500,SECRET,{},io.BytesIO(SECRET.encode()))
    monkeypatch.setattr(a.urllib.request,'build_opener',lambda *a:Opener())
    assert a.request(8787,'/health',None,None,150)==(None,500,'HTTP_ERROR')


def test_no_app_import_in_standalone_source():
    import ast
    tree=ast.parse(Path(a.__file__).read_text())
    assert all(not (isinstance(n,ast.ImportFrom) and (n.module or '').startswith('app')) for n in ast.walk(tree))


@pytest.mark.parametrize('flag,value',[('--symbols','SECRET_SENTINEL!'),('--sample-size','49'),('--timeout','nan')])
def test_invalid_arguments_complete_neutral_schema(tmp_path,capsys,flag,value):
    code=a.main(['--execute',flag,value,'--log',str(tmp_path/'j'),'--run-log',str(tmp_path/'r')],requester=lambda *a:pytest.fail('request'))
    captured=capsys.readouterr();result=json.loads(captured.out)
    assert code==78 and result=={'mode':'EXECUTE','status':'BLOCKED','symbols':[],'counts':{},'requests':0,'health':'NOT_CHECKED','code':'INVALID_ARGUMENTS'}
    assert 'SECRET_SENTINEL' not in captured.out+captured.err and not list(tmp_path.iterdir())

@pytest.mark.parametrize('source',['env','stdin'])
def test_invalid_token_complete_neutral_schema(tmp_path,monkeypatch,capsys,source):
    import io
    token='SECRET_żółw';monkeypatch.setenv('ADMIN_TOKEN',token);monkeypatch.setattr(a.sys,'stdin',io.StringIO(token+'\n'))
    code=a.main(['--execute','--token-'+source,'--log',str(tmp_path/'j'),'--run-log',str(tmp_path/'r')],requester=lambda *a:pytest.fail('request'))
    captured=capsys.readouterr();result=json.loads(captured.out)
    assert code==78 and result=={'mode':'EXECUTE','status':'BLOCKED','symbols':[],'counts':{},'requests':0,'health':'NOT_CHECKED','code':'INVALID_ACCESS_TOKEN'}
    assert token not in captured.out+captured.err and not list(tmp_path.iterdir())

@pytest.mark.parametrize('source',['env','stdin','none'])
def test_main_real_transport_headers(tmp_path,monkeypatch,capsys,source):
    import io
    token='TRANSPORT_TOKEN_café';monkeypatch.setenv('ADMIN_TOKEN',token);monkeypatch.setattr(a.sys,'stdin',io.StringIO(token+'\n'));calls=[]
    class Response:
        status=200
        def __init__(self,data):self.data=data
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,n):return json.dumps(self.data).encode()
    class Opener:
        def open(self,req,timeout):
            calls.append(req)
            headers={k.lower():v for k,v in req.header_items()}
            assert headers['content-type']=='application/json' and 'origin' not in headers
            assert headers.get('x-admin-token')==(token if source!='none' else None)
            if req.full_url.endswith('/health'):return Response({'status':'healthy','typesafe_configured':True,'twitter_configured':True})
            assert req.full_url=='http://127.0.0.1:8787/api/v1/analyze'
            assert json.loads(req.data)=={'symbol':'ETH','sample_size':50}
            return Response({'status':'unavailable','decision_logged':True,'record_id':'a'*32})
    monkeypatch.setattr(a.urllib.request,'build_opener',lambda *args:Opener())
    args=['--execute','--symbols','ETH','--log',str(tmp_path/'j'),'--run-log',str(tmp_path/'r')]
    if source!='none':args+=['--token-'+source]
    assert a.main(args,clock=lambda:NOW)==0 and len(calls)==2
    captured=capsys.readouterr();assert token not in captured.out+captured.err+Path(tmp_path/'r').read_text()

@pytest.mark.parametrize('wrapped',[False,True])
def test_request_timeout_mapping_no_retry(monkeypatch,wrapped):
    calls=[]
    class Opener:
        def open(self,*args,**kwargs):
            calls.append(1);error=TimeoutError(SECRET)
            raise urllib.error.URLError(error) if wrapped else error
    monkeypatch.setattr(a.urllib.request,'build_opener',lambda *args:Opener())
    assert a.request(8787,'/api/v1/analyze',{'symbol':'ETH','sample_size':50},None,150)==(None,None,'TIMEOUT')
    assert len(calls)==1
