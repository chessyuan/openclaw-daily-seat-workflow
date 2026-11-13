"""Bounded recovery for explicitly rejected WeChat notifications; no credentials stored."""
import contextlib,fcntl,hashlib,importlib.machinery,importlib.util,json,os,time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

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

def terminal_update(record):
    """Replace an explicitly rejected progress warning with its actual outcome."""
    message=str(record.get('message') or '')
    if not ('尚未完成签到' in message or message.startswith(('⚠ 手机节点离线','⚠ 预约视觉服务异常'))):
        return record
    tz=ZoneInfo('Asia/Shanghai')
    try:
        created=float(record['createdAt']);day=datetime.fromtimestamp(created,tz).strftime('%Y%m%d')
        paths=sorted((ROOT.parent/'runs').glob('checkin-'+day+'-*.json'),reverse=True)
        for path in paths[:8]:
            run=json.loads(path.read_text())
            start=datetime.strptime(run['startedAt'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=tz).timestamp()
            end=datetime.strptime(run['endedAt'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=tz).timestamp()
            if not start<=created<=end:continue
            status=run.get('status')
            if status=='passed':text='✅ 预约与签到已完成。'
            elif status=='reservation_pending_checkin':text='🚨 可能已预约，但签到未确认。禁止重复选座，请立即核实并人工签到。'
            elif status=='failed':
                stage=run.get('failedStage') or {};node=stage.get('nodeConnection') or {}
                if stage.get('stage') in ('node','model_preflight') and node.get('connected') is False:
                    text='❌ 预约签到未完成：手机节点持续离线，未能进入微信预约。请打开手机OpenClaw检查连接。'
                else:text='❌ 预约签到未完成：流程未形成已确认闭环，请核实本人预约与签到状态。'
            else:continue
            return {**record,'message':text+'\n结束时间：'+run['endedAt'],'terminalRun':path.name}
    except (OSError,ValueError,KeyError,TypeError):pass
    return record

def flush(send):
    counts={'sent':0,'waiting':0,'expired':0,'rejected':0,'unknown':0}
    with locked():
        for path in list(ROOT.glob('*.json'))[:8]:
            record=json.loads(path.read_text(encoding='utf-8'))
            if time.time()-record['createdAt']>24*3600 or record.get('attempts',0)>=3:
                path.unlink();counts['expired']+=1;continue
            updated=terminal_update(record)
            if updated!=record:
                record=updated;write_record(path,record)
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
    loader=importlib.machinery.SourceFileLoader('signin',str(Path.home()/'.local/bin/android-signin'))
    spec=importlib.util.spec_from_loader(loader.name,loader)
    module=importlib.util.module_from_spec(spec);loader.exec_module(module)
    # Reuse the existing one-minute service, even when its outbox is empty.
    # No new business task, phone action or Gateway restart is performed here.
    try:
        readiness_loader=importlib.machinery.SourceFileLoader('android_readiness',str(Path(__file__).with_name('android-readiness.py')))
        readiness_spec=importlib.util.spec_from_loader(readiness_loader.name,readiness_loader)
        readiness=importlib.util.module_from_spec(readiness_spec);readiness_loader.exec_module(readiness)
        readiness.tick(module)
    except Exception as exc:
        print(json.dumps({'androidReadinessError':type(exc).__name__}),flush=True)
    if not ROOT.exists() or not any(ROOT.glob('*.json')):raise SystemExit(0)
    result=flush(module._notify_wechat_once)
    if any(result[k] for k in ('sent','expired','rejected','unknown')):
        print(json.dumps(result),flush=True) # counts only; no tokens/messages
