"""Read-only Android readiness and one bounded pre-booking warning per day."""
import fcntl,json,os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Shanghai')
JOB=os.environ.get('ANDROID_CHECKIN_JOB_ID', '')
REQUIRED={'mobile.ui.observe','mobile.ui.act','mobile.ui.launch','mobile.ui.screenshot'}

def save(path,value):
    tmp=path.with_suffix('.tmp')
    with tmp.open('w',encoding='utf-8') as stream:
        os.chmod(tmp,0o600);json.dump(value,stream,ensure_ascii=False)
        stream.flush();os.fsync(stream.fileno())
    os.replace(tmp,path)

def classify(payload):
    if payload.get('ok') is not True or not isinstance(payload.get('nodes'),list):
        return 'unknown',{}
    nodes=[n for n in payload['nodes'] if n.get('displayName')=='my-android-phone']
    if len(nodes)!=1:return 'unknown',{}
    node=nodes[0]
    safe={k:node.get(k) for k in ('lastSeenAtMs','lastConnectedAtMs','lastSeenReason')}
    if node.get('connected') is False:return 'offline',safe
    if node.get('connected') is not True:return 'unknown',safe
    if not REQUIRED.issubset(set(node.get('commands') or [])):return 'capability_missing',safe
    return 'online',safe

def destination(job):
    if not job.get('enabled'):return None
    argv=job.get('payload',{}).get('argv') or []
    if not argv or Path(argv[0]).name!='android-signin':return None
    try:
        values=[argv[argv.index(flag)+1] for flag in ('--notify-channel','--notify-account','--notify-target')]
    except (ValueError,IndexError):return None
    return values if all(values) else None

def tick(signin,now=None):
    now=now or datetime.now(TZ)
    now=now.astimezone(TZ);stamp=now.timestamp();day=now.strftime('%Y-%m-%d')
    root=signin.ROOT;root.mkdir(parents=True,exist_ok=True)
    path=root/'android-readiness.json'
    with (root/'android-readiness.lock').open('a') as lock:
        os.chmod(lock.name,0o600)
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'state':'busy','notified':False}
        try:state=json.loads(path.read_text())
        except (OSError,ValueError):state={}
        if not isinstance(state,dict):state={}
        try:
            payload=signin.run_json([str(signin.NODE_BIN),str(signin.FAST_ACT),'status'],timeout=8)
            status,safe=classify(payload);reason=status
        except Exception as exc:
            status,safe,reason='unknown',{},type(exc).__name__
        previous_state=state.get('state')
        if status in ('offline','capability_missing'):
            if state.get('lastKnownState',previous_state) not in ('offline','capability_missing'):
                state['offlineSince']=stamp
            state.setdefault('offlineSince',state.get('since',stamp))
            state['lastKnownState']=status
        elif status=='online':
            state['lastKnownState']='online';state.pop('offlineSince',None)
        # A transient query timeout is unknown, not proof of recovery; it must
        # not erase a previously observed offline interval or delay its alert.
        if status!=previous_state:
            history=state.get('transitions',[])[-11:]
            state.update(state=status,since=stamp,transitions=history+[{'at':now.isoformat(),'state':status}])
        state.update(checkedAt=now.isoformat(),reason=reason,node=safe)
        # This is not a second booking scheduler: it only checks and warns.
        # Never restart Gateway/Windows or attempt to operate an offline phone.
        minutes=now.hour*60+now.minute
        should_warn=(380<=minutes<403 and status in ('offline','capability_missing')
                     and stamp-state.get('offlineSince',stamp)>=60 and state.get('alertDay')!=day)
        if should_warn:
            try:
                job=signin.run_json([signin.OPENCLAW,'cron','get',JOB,'--json'],timeout=12)
                route=destination(job)
                if route:
                    # Reserve before network send; ambiguous sends cannot spam.
                    state.update(alertDay=day,delivery={'sent':False,'reason':'reserved_before_send'})
                    save(path,state)
                    reason_text='手机节点离线' if status=='offline' else '手机自动化能力不完整'
                    result=signin.notify_wechat('⚠ 今日预约准备检查：'+reason_text+'。'
                        '请打开手机上的OpenClaw并确认已连接，检查后台和VPN。'
                        '06:28仍会按原计划启动；现在没有预约、签到或更改手机设置。',*route)
                    state['delivery']={k:result.get(k) for k in ('sent','reason','queued') if k in result}
            except Exception as exc:
                state['warningError']=type(exc).__name__
        save(path,state)
        return {'state':status,'checkedAt':now.isoformat(),'alertDay':state.get('alertDay'),
                'delivery':state.get('delivery')}
