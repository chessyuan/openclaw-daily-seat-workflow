"""Bounded recovery for explicitly rejected WeChat notifications; no credentials stored."""
import contextlib,fcntl,hashlib,importlib.machinery,importlib.util,json,os,time
from pathlib import Path

ROOT=Path.home()/'.local/share/openclaw-android-workflows/notification-outbox'
CONTEXT=Path.home()/'.openclaw/openclaw-weixin/accounts'

def fingerprint(account):
    if not account or Path(account).name!=account:return None
    try:return hashlib.sha256((CONTEXT/(account+'.context-tokens.json')).read_bytes()).hexdigest()
    except OSError:return None

@contextlib.contextmanager
def locked():
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(ROOT,0o700)
    with (ROOT/'.lock').open('a') as stream:
        os.chmod(ROOT/'.lock',0o600)
        fcntl.flock(stream,fcntl.LOCK_EX)
        yield

def record_path(account,target):
    return ROOT/(hashlib.sha256((account+'\0'+target).encode()).hexdigest()+'.json')

def write_record(path,record):
    temporary=path.with_suffix('.tmp')
    with temporary.open('w',encoding='utf-8') as stream:
        os.chmod(temporary,0o600)
        json.dump(record,stream,ensure_ascii=False)
    os.replace(temporary,path)

def track(result,message,channel,account,target):
    if not (account and target):return result
    # Unknown delivery states must never be replayed. Only a definitive
    # prepare rejection permits queueing; newest result supersedes old warnings.
    with locked():
        path=record_path(account,target)
        if result.get('sent'):
            path.unlink(missing_ok=True)
        elif result.get('reason')=='wechat_upstream_prepare_rejected':
            write_record(path,{'createdAt':time.time(),'channel':channel,'account':account,
                'target':target,'message':message,'contextFingerprint':fingerprint(account),
                'attempts':0})
            result={**result,'queued':True,'retryWhen':'fresh_wechat_context'}
    return result

def flush(send):
    counts={'sent':0,'waiting':0,'expired':0,'rejected':0,'unknown':0}
    with locked():
        for path in list(ROOT.glob('*.json'))[:8]:
            record=json.loads(path.read_text(encoding='utf-8'))
            if time.time()-record['createdAt']>24*3600 or record.get('attempts',0)>=3:
                path.unlink();counts['expired']+=1;continue
            current=fingerprint(record['account'])
            if not current or current==record.get('contextFingerprint'):
                counts['waiting']+=1;continue
            # Persist BEFORE sending. A crash/timeout cannot repeatedly replay
            # against the same context. Hold the lock to prevent stale replacement.
            record.update(contextFingerprint=current,attempts=record.get('attempts',0)+1)
            write_record(path,record)
            result=send('【延迟补发】\n'+record['message'],record['channel'],record['account'],record['target'])
            if result.get('sent'):
                path.unlink();counts['sent']+=1
            elif result.get('reason')=='wechat_upstream_prepare_rejected':
                counts['rejected']+=1
            else:
                # Timeout/auth/unknown: retire rather than risk duplicates.
                path.unlink();counts['unknown']+=1
    return counts

if __name__=='__main__':
    if not ROOT.exists() or not any(ROOT.glob('*.json')):raise SystemExit(0)
    loader=importlib.machinery.SourceFileLoader('signin',str(Path.home()/'.local/bin/android-signin'))
    spec=importlib.util.spec_from_loader(loader.name,loader)
    module=importlib.util.module_from_spec(spec);loader.exec_module(module)
    result=flush(module._notify_wechat_once)
    if any(result[k] for k in ('sent','expired','rejected','unknown')):
        print(json.dumps(result),flush=True) # counts only; no tokens/messages
