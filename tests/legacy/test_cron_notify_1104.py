import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from zoneinfo import ZoneInfo

spec=importlib.util.spec_from_file_location('outbox','/tmp/android-notification-outbox-1104.py')
o=importlib.util.module_from_spec(spec)
spec.loader.exec_module(o)

class CronRecovery(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);o.ROOT=self.root/'notification-outbox';o.CONTEXT=self.root/'contexts'
        o.CONTEXT.mkdir();self.context=o.CONTEXT/'bot.context-tokens.json'
        self.context.write_text('{"user":"TEST_CONTEXT_SECRET"}')
        self.now=datetime(2026,11,4,8,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.start=self.now-5520;self.duration=267545
        os.utime(self.context,(self.start-60,self.start-60))
        runs=self.root/'runs';runs.mkdir();self.run=runs/'checkin-20261104-063225.json'
        self.run_data={'status':'passed','startedAt':'2026-11-04 06:28:00','endedAt':'2026-11-04 06:31:40',
            'result':{'success':True},'selectedSeat':'博学楼四楼半圆弧平7号'}
        self.run.write_text(json.dumps(self.run_data))
        self.job={'id':'aad8e69d-068b-4e58-82b8-6b7f1c2db970','enabled':True,
            'payload':{'argv':['/home/test/.local/bin/android-signin','--notify-channel','openclaw-weixin',
                '--notify-account','bot','--notify-target','user']},
            'delivery':{'mode':'announce','channel':'openclaw-weixin','accountId':'bot','to':'user'},
            'state':{'lastRunAtMs':int(self.start*1000),'lastDurationMs':self.duration,
                'lastRunStatus':'ok','lastDeliveryStatus':'not-delivered',
                'lastDeliveryError':'WeChat message prepare failed (ret=-2)'}}
        self.rpc=Mock(return_value=self.job)
        self.signin=SimpleNamespace(ROOT=self.root,OPENCLAW='/bin/test-claw',run_json=self.rpc)

    def collect(self):
        # The stable baseline has no collector: treat that as the observable
        # missing behavior so the pre-fix test fails on the dropped notice.
        fn=getattr(o,'collect_cron_failure',lambda *a,**k:{'queued':False})
        with patch.object(o.time,'time',return_value=self.now):
            return fn(self.signin)

    def records(self):return list(o.ROOT.glob('*.json'))

    def test_explicit_failed_cron_announcement_is_queued(self):
        self.assertTrue(self.collect()['queued'])
        data=json.loads(self.records()[0].read_text())
        self.assertIn('06:31:40',data['message']);self.assertIn('已完成',data['message'])
        self.assertIn('sourceCronKey',data)
        self.assertNotIn('TEST_CONTEXT_SECRET',self.records()[0].read_text())

    def test_old_context_waits_without_sending(self):
        self.assertTrue(self.collect()['queued'])
        with patch.object(o.time,'time',return_value=self.now):
            self.assertEqual(o.flush(lambda *a:self.fail('Old context must wait'))['waiting'],1)

    def test_context_refreshed_since_failure_can_send_once(self):
        os.utime(self.context,(self.now,self.now))
        self.assertTrue(self.collect()['queued'])
        sent=[]
        with patch.object(o.time,'time',return_value=self.now):
            self.assertEqual(o.flush(lambda *a:sent.append(a) or {'sent':True})['sent'],1)
        self.collect()
        self.assertEqual(len(sent),1);self.assertEqual(self.records(),[])

    def test_delivery_unknown_never_replayed(self):
        self.job['state']['lastDeliveryError']='delivery timeout, outcome unknown'
        self.collect();self.assertEqual(self.records(),[])

    def test_delivered_never_replayed(self):
        self.job['state']['lastDeliveryStatus']='delivered'
        self.collect();self.assertEqual(self.records(),[])

    def test_inconsistent_success_is_not_invented(self):
        self.run_data['result']['success']=False;self.run.write_text(json.dumps(self.run_data))
        self.collect();self.assertEqual(self.records(),[])

    def test_unmatched_cron_run_is_not_replayed(self):
        self.job['state']['lastRunAtMs']-=3600*1000
        self.collect();self.assertEqual(self.records(),[])

    def test_disabled_schedule_is_not_replayed(self):
        self.job['enabled']=False
        self.collect();self.assertEqual(self.records(),[])

    def test_route_mismatch_is_not_replayed(self):
        self.job['delivery']['to']='different-recipient'
        self.collect();self.assertEqual(self.records(),[])

    def test_newer_notice_is_preserved(self):
        with patch.object(o.time,'time',return_value=self.now):
            o.track({'sent':False,'reason':'wechat_upstream_prepare_rejected'},'newer notification','openclaw-weixin','bot','user')
        self.collect()
        self.assertEqual(json.loads(self.records()[0].read_text())['message'],'newer notification')

    def test_terminal_unknown_send_is_not_requeued(self):
        os.utime(self.context,(self.now,self.now))
        self.assertTrue(self.collect()['queued'])
        with patch.object(o.time,'time',return_value=self.now):
            self.assertEqual(o.flush(lambda *a:{'sent':False,'reason':'delivery_timeout_unknown_do_not_replay'})['unknown'],1)
        self.collect();self.assertEqual(self.records(),[])

    def test_completed_key_avoids_query_every_minute(self):
        self.assertTrue(self.collect()['queued'])
        self.rpc.reset_mock();self.collect();self.rpc.assert_not_called()

    def test_no_completed_run_no_query(self):
        self.run.unlink();self.collect();self.rpc.assert_not_called()

    def test_running_job_is_deferred(self):
        self.job['state']['runningAtMs']=self.job['state']['lastRunAtMs']
        self.collect();self.assertEqual(self.records(),[])

    def test_queue_without_marker_is_not_reset_after_partial_write(self):
        self.assertTrue(self.collect()['queued'])
        marker=self.root/'cron-notification-recovery.json';marker.unlink()
        path=self.records()[0];data=json.loads(path.read_text());data['attempts']=2;path.write_text(json.dumps(data))
        self.collect();self.assertEqual(json.loads(path.read_text())['attempts'],2)

    def test_state_write_failure_defers_send_and_recovers_once(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory);base=home/'.local/share/openclaw-android-workflows'
            (base/'runs').mkdir(parents=True)
            (base/'runs'/self.run.name).write_text(json.dumps(self.run_data))
            (base/'job.json').write_text(json.dumps(self.job))
            context=home/'.openclaw/openclaw-weixin/accounts'
            context.mkdir(parents=True);(context/'bot.context-tokens.json').write_text('{"user":"TEST_CONTEXT"}')
            binaries=home/'.local/bin';binaries.mkdir(parents=True)
            program=binaries/'android-notification-outbox.py';shutil.copy2(o.__file__,program)
            (binaries/'android-readiness.py').write_text('def tick(*args): pass\n')
            (binaries/'android-signin').write_text(
                'import json\nfrom pathlib import Path\nROOT=Path.home()/".local/share/openclaw-android-workflows"\n'
                'OPENCLAW="/never-execute"\n'
                'def run_json(*args,**kwargs): return json.loads((ROOT/"job.json").read_text())\n'
                'def _notify_wechat_once(*args):\n'
                ' p=ROOT/"send-count"\n'
                ' p.write_text(str(int(p.read_text())+1) if p.exists() else "1")\n'
                ' return {"sent":True}\n')
            blocker=base/'cron-notification-recovery.json';blocker.mkdir()
            code=('from pathlib import Path;import runpy,time;'
                f'Path.home=classmethod(lambda cls:Path({str(home)!r}));time.time=lambda:{self.now!r};'
                f'runpy.run_path({str(program)!r},run_name="__main__")')
            result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertFalse((base/'send-count').exists(),'Do not send without durable run marker')
            blocker.rmdir()
            for _ in range(2):
                result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual((base/'send-count').read_text(),'1')


if __name__=='__main__':unittest.main()
