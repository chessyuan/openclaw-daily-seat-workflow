"""Exercise the archived runner in isolated subprocesses."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='seat-workflow-tests-') as folder:
    temp = Path(folder)
    runner = root/'src/android-signin.py'
    tests = sorted((root/'tests/legacy').glob('test_*.py'))
    shutil.copyfile(runner,temp/'android-signin-audit.py')
    shutil.copyfile(root/'tests/legacy/android-notification-outbox.py',temp/'android-notification-outbox.py')
    (temp/'test_runner.py').write_text("""import datetime, runpy, socket, sys, time
base = datetime.datetime(2026,11,13,4,0,tzinfo=datetime.timezone.utc)
now = time.monotonic()
class Clock(datetime.datetime):
    @classmethod
    def now(cls,tz=None):
        value=base+datetime.timedelta(seconds=time.monotonic()-now)
        return value.astimezone(tz) if tz else value.astimezone().replace(tzinfo=None)
datetime.datetime=Clock
def no_network(*args,**kwargs):
    raise AssertionError("network unavailable in test")
socket.create_connection=no_network
socket.socket.connect=no_network
target=sys.argv.pop(1)
sys.argv[0]=target
runpy.run_path(target,run_name="__main__")
""", encoding='utf-8')
    env = dict(os.environ, SIGNIN_SOURCE=str(runner), HOME=str(temp),
               ANDROID_CHECKIN_JOB_ID='test-job')
    failures = []
    count = 0
    for test in tests:
        s = test.read_text(encoding='utf-8')
        s = re.sub(r'/tmp/android-signin[-\w]+\.py',lambda _:str(runner),s)
        s = s.replace('__ARCHIVED_RUNNER__',str(runner))
        for old,new in (
            ('/tmp/android-readiness.py',root/'src/android-readiness.py'),
            ('/tmp/android-notification-outbox-1104.py',root/'src/android-notification-outbox.py'),
            ('/tmp/android-notification-outbox-1027.py',root/'tests/legacy/android-notification-outbox-1027.py'),
            ('/tmp/workflow-matrix-1103.json',temp/'workflow-matrix-1103.json'),
        ):
            s=s.replace(old,str(new))
        path=temp/test.name
        path.write_text(s,encoding='utf-8')
        run=subprocess.run([sys.executable,str(temp/'test_runner.py'),str(path)],env=env,
                           capture_output=True,text=True,timeout=65)
        output=run.stdout+run.stderr
        n=re.search(r'Ran (\d+) tests?',output)
        count+=int(n[1]) if n else 0
        print(f'{test.name}: {"PASS" if run.returncode == 0 else "FAIL"}',flush=True)
        if run.returncode:
            failures.append(test.name)
            print(output[-5200:],flush=True)
    print(f'{count} Python unittest cases; {len(failures)} failing suites.')
    raise SystemExit(bool(failures))
