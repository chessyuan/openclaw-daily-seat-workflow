import importlib.machinery,importlib.util,subprocess,json
from unittest.mock import patch
loader=importlib.machinery.SourceFileLoader('signin','__ARCHIVED_RUNNER__')
spec=importlib.util.spec_from_loader(loader.name,loader)
m=importlib.util.module_from_spec(spec);loader.exec_module(m)
def call(p):
    with patch.object(m.subprocess,'run',return_value=p):return m._notify_wechat_once('test','openclaw-weixin','account','recipient')
def proc(obj=None,rc=0,err='',prefix=''):
    return subprocess.CompletedProcess([],rc,prefix+(json.dumps(obj) if obj is not None else ''),err)
assert call(proc({'action':'send','messageId':'id','payload':{'deliveryStatus':'sent'}}))['sent']
assert call(proc({'action':'send','messageId':'id','payload':{'deliveryStatus':'failed'}}))['sent'] is False
assert call(proc({'action':'send','messageId':'','payload':{'deliveryStatus':'sent'}}))['sent'] is False
assert call(proc())['sent'] is False
assert call(proc(rc=1,err='sendMessage ret=-2 errmsg=prepare failed'))['reason']=='wechat_upstream_prepare_rejected'
assert call(proc(rc=1,err='ret=-14'))['reason']=='wechat_auth_expired'
assert call(proc(rc=1,err='timeout'))['reason']=='delivery_timeout_unknown_do_not_replay'
assert call(proc({'action':'send','messageId':'id','payload':{'deliveryStatus':'sent'}},prefix='Warning: {not JSON}\n'))['sent']
with patch.object(m.subprocess,'run',side_effect=subprocess.TimeoutExpired('test',45)):
    assert not m._notify_wechat_once('test','openclaw-weixin','account','recipient')['sent']
assert not m._notify_wechat_once('test','','','')['sent']
print('TOTAL_PASS 10 notification acknowledgement tests (no live sends)')
