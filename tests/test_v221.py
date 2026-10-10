"""Synthetic v2.2.1 collector/fingerprint regressions; no network or real data."""
import ast
import errno
import json
import os
from pathlib import Path
import urllib.error
import pytest
from app.core import decision_log as journal
from scripts import collect_candles as c
from test_collector_kraken_format import payload
from test_protocol_v22 import sources
from test_research import write

SECRET='PRIVATE_URL_PAYLOAD_EXCEPTION_SENTINEL'


def inject(source,literal):
    tree=ast.parse(source)
    method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='process_tweets')
    method.body.append(ast.parse('literal_probe = '+literal).body[0])
    return ast.unparse(tree)

@pytest.mark.parametrize('literal',["b'x'",'...','1j','1e999j'])
def test_input_special_literals(literal):
    original=sources();changed=original.copy();changed[0]=inject(changed[0],literal)
    assert journal.inputs_fingerprint(*changed)!=journal.inputs_fingerprint(*original)

@pytest.mark.parametrize('literal',["b'x'",'...','1j'])
def test_prompt_special_literals(literal):
    source=(journal.ROOT/'app/services/typesafe_service.py').read_text();tree=ast.parse(source)
    state=next(n for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(x,ast.Name) and x.id=='state' for x in n.targets))
    state.value.keys.append(ast.Constant(value='literal_probe'));state.value.values.append(ast.parse(literal,mode='eval').body)
    assert journal.prompt_fingerprint(ast.unparse(tree))!=journal.prompt_fingerprint(source)


def test_special_literals_do_not_collide():
    original=sources();hashes=[]
    for literal in ('1j','[0.0,1.0]',"'...'",'...',"b'x'","{'bytes':'78'}"):
        changed=original.copy();changed[0]=inject(changed[0],literal);hashes.append(journal.inputs_fingerprint(*changed))
    assert len(set(hashes))==len(hashes)


def run(tmp_path,fetcher,cache=None):
    log=tmp_path/'log';write(log,[])
    return c.collect(log,cache or tmp_path/'cache',now=1791500100+300*722,execute=True,fetcher=fetcher,sleep=lambda _:None,track='ETH')

@pytest.mark.parametrize('case,expected',[
    ('pair','PAIR_UNKNOWN'),('http','PROVIDER_HTTP_ERROR'),('timeout','PROVIDER_TRANSPORT_ERROR'),
    ('url','PROVIDER_TRANSPORT_ERROR'),('limit','PROVIDER_INVALID_PAYLOAD'),('schema','PROVIDER_INVALID_PAYLOAD'),
    ('cache','CACHE_INVALID'),('write','STORE_WRITE_ERROR'),('fsync','STORE_WRITE_ERROR')])
def test_collector_closed_codes(tmp_path,monkeypatch,capsys,caplog,case,expected):
    if case=='cache':
        directory=tmp_path/'cache';directory.mkdir();(directory/'ETH_USD_5m.jsonl').write_text(SECRET)
    if case=='write':monkeypatch.setattr(c,'append_cache',lambda *a:(_ for _ in ()).throw(OSError(errno.ENOSPC,SECRET)))
    if case=='fsync':
        directory=tmp_path/'cache';directory.mkdir()
        monkeypatch.setattr(c.os,'fsync',lambda *a:(_ for _ in ()).throw(OSError(errno.ENOSPC,SECRET)))
    def fetch(pair):
        if case=='pair':return {'error':['EQuery:Unknown asset pair '+SECRET]}
        if case=='http':raise urllib.error.HTTPError('https://'+SECRET,429,SECRET,{},None)
        if case=='timeout':raise TimeoutError(SECRET)
        if case=='url':raise urllib.error.URLError(SECRET)
        if case=='limit':return payload(722)
        if case=='schema':return {'error':[],'result':SECRET}
        return payload(2)
    result=run(tmp_path,fetch)
    assert result['pair_results']['ETH/USD']['code']==expected
    assert result['status'] in ('PARTIAL','BLOCKED')
    if case=='http':assert result['pair_results']['ETH/USD']['http_status']==429
    captured=capsys.readouterr();assert SECRET not in json.dumps(result)+captured.out+captured.err+caplog.text


def test_fetch_non200_and_invalid_json(tmp_path,monkeypatch):
    class Response:
        status=503
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,*args):return SECRET.encode()
    class Opener:
        def open(self,request,timeout):
            assert 'pair=XBTUSD' in request.full_url or 'pair=ETHUSD' in request.full_url
            return Response()
    monkeypatch.setattr(c.urllib.request,'build_opener',lambda *args:Opener())
    result=run(tmp_path,c.fetch)
    assert result['pair_results']['ETH/USD']=={'status':'FAILED','code':'PROVIDER_HTTP_ERROR','http_status':503}
    Response.status=200
    result=run(tmp_path,c.fetch)
    assert result['pair_results']['ETH/USD']['code']=='PROVIDER_INVALID_PAYLOAD'
    assert SECRET not in json.dumps(result)


def test_store_mode_and_dryrun_no_writes(tmp_path):
    log=tmp_path/'log';write(log,[]);cache=tmp_path/'new'/'cache';before=set(tmp_path.iterdir())
    result=c.collect(log,cache,track='ETH')
    assert result['requests']==0 and not cache.exists() and set(tmp_path.iterdir())==before
    result=run(tmp_path,lambda _:payload(2),cache)
    assert result['status']=='OK' and cache.stat().st_mode&0o777==0o700
    assert cache.parent.stat().st_mode&0o777==0o700
    assert all(p.stat().st_mode&0o777==0o600 for p in cache.iterdir())

@pytest.mark.parametrize('kind',['leaf','parent','file','cachefile','fifo'])
def test_no_symlink_traversal_or_special_file(tmp_path,kind):
    outside=tmp_path/'outside';outside.mkdir();cache=tmp_path/'store';calls=[]
    if kind=='leaf':cache.symlink_to(outside,target_is_directory=True)
    elif kind=='parent':
        parent=tmp_path/'parent';parent.symlink_to(outside,target_is_directory=True);cache=parent/'store'
    elif kind=='file':cache.write_text(SECRET)
    else:
        cache.mkdir()
        if kind=='cachefile':(outside/'target').write_text(SECRET);(cache/'ETH_USD_5m.jsonl').symlink_to(outside/'target')
        else:os.mkfifo(cache/'ETH_USD_5m.jsonl',0o600)
    before={str(p):p.read_bytes() for p in outside.iterdir() if p.is_file()}
    def fetch(pair):calls.append(pair);return payload(2)
    result=run(tmp_path,fetch,cache)
    assert result['pair_results']['ETH/USD']['status']=='FAILED'
    assert 'ETH/USD' not in calls
    assert {str(p):p.read_bytes() for p in outside.iterdir() if p.is_file()}==before
    assert not (outside/'store').exists()


def test_error_does_not_abort_other_pair(tmp_path):
    def fetch(pair):return {'error':['EQuery:Unknown asset pair']} if pair=='XBT/USD' else payload(2)
    result=run(tmp_path,fetch)
    assert result['status']=='PARTIAL' and result['pair_results']['ETH/USD']['status']=='OK'


def test_docstrings_ignored_but_nonleading_strings_retained():
    original=sources();expected=journal.inputs_fingerprint(*original)
    for change in ('edit','remove','add'):
        tree=ast.parse(original[0]);method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='process_tweets')
        if ast.get_docstring(method) is not None:method.body.pop(0)
        if change!='remove':method.body.insert(0,ast.Expr(value=ast.Constant(value='different docstring '+change)))
        changed=original.copy();changed[0]=ast.unparse(tree)
        assert journal.inputs_fingerprint(*changed)==expected
    # Indentation changes docstring contents, but not executable syntax.
    import textwrap
    changed=original.copy();changed[0]='if True:\n'+textwrap.indent(original[0],'    ')
    # Parse the module then unwrap the syntactic wrapper, retaining altered docs.
    tree=ast.parse(changed[0]);changed[0]=ast.unparse(ast.Module(body=tree.body[0].body,type_ignores=[]))
    assert journal.inputs_fingerprint(*changed)==expected
    for text in ('first semantic string','second semantic string'):
        tree=ast.parse(original[0]);method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='process_tweets')
        method.body.append(ast.Expr(value=ast.Constant(value=text)));changed=original.copy();changed[0]=ast.unparse(tree)
        assert journal.inputs_fingerprint(*changed)!=expected


def test_nested_docstrings_ignored():
    original=sources();hashes=[]
    for text in ('one','two'):
        tree=ast.parse(original[0]);method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='process_tweets')
        method.body.extend(ast.parse('class Nested:\n    "'+text+'"\n    async def f(self):\n        "'+text+'"\n        return 1').body)
        changed=original.copy();changed[0]=ast.unparse(tree);hashes.append(journal.inputs_fingerprint(*changed))
    assert hashes[0]==hashes[1]


def test_greed_semantics_still_versioned():
    original=sources();changed=original.copy();changed[0]=changed[0].replace('GREED_KEYWORDS = {','GREED_KEYWORDS = {"semantic_new_word",',1)
    assert journal.inputs_fingerprint(*original)!=journal.inputs_fingerprint(*changed)


def test_protocol_numbering():
    import re
    document=(journal.ROOT/'RESEARCH-PROTOCOL.md').read_text()
    assert re.findall(r'^(\d+)\. ',document,re.M)==list(map(str,range(1,15)))
    assert '12. Exploratory sign permutation:' in document

@pytest.mark.parametrize('failure',['fchmod','fsync'])
def test_directory_descriptors_closed_after_failure(tmp_path,monkeypatch,failure):
    opened=set();real_open=c.os.open;real_close=c.os.close
    def tracked_open(*args,**kwargs):
        fd=real_open(*args,**kwargs);opened.add(fd);return fd
    def tracked_close(fd):opened.discard(fd);return real_close(fd)
    monkeypatch.setattr(c.os,'open',tracked_open);monkeypatch.setattr(c.os,'close',tracked_close)
    monkeypatch.setattr(c.os,failure,lambda *args:(_ for _ in ()).throw(OSError(errno.ENOSPC,SECRET)))
    with pytest.raises(OSError):c.store_directory(tmp_path/'new-store')
    assert not opened


def test_append_failure_preserves_stored_prefix(tmp_path,monkeypatch):
    run(tmp_path,lambda _:payload(2));cache=tmp_path/'cache';before={p.name:p.read_bytes() for p in cache.iterdir()}
    real_write=c.os.write;called=[False]
    def fail(fd,data):
        if not called[0]:called[0]=True;return real_write(fd,data[:5])
        raise OSError(errno.ENOSPC,SECRET)
    monkeypatch.setattr(c.os,'write',fail)
    result=run(tmp_path,lambda _:payload(3))
    assert result['status']=='BLOCKED'
    for name,content in before.items():assert (cache/name).read_bytes().startswith(content)
