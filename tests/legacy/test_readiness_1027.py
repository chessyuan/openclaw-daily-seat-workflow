import importlib.util,json,tempfile,unittest
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
r=load('readiness','/tmp/android-readiness.py')
o=load('outbox','/tmp/android-notification-outbox-1027.py')

class Readiness(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.now=datetime(2026,10,27,6,20,1,tzinfo=r.TZ)
        self.node=dict(displayName='my-android-phone',connected=False,commands=list(r.REQUIRED))
        self.job=dict(enabled=True,payload={'argv':['/home/test/.local/bin/android-signin','--notify-channel','weixin','--notify-account','bot','--notify-target','recipient']})
        self.signin=SimpleNamespace(ROOT=self.root,NODE_BIN='/bin/testnode',FAST_ACT='/bin/testhelper',OPENCLAW='/bin/testclaw',
            run_json=Mock(side_effect=lambda argv,**kw: self.job if 'cron' in argv else {'ok':True,'nodes':[dict(self.node)]}),
            notify_wechat=Mock(return_value={'sent':True}))
    def prime(self):r.tick(self.signin,self.now-timedelta(seconds=62))
    def test_offline_detected_without_actions(self):
        self.assertEqual(r.tick(self.signin,self.now)['state'],'offline');self.signin.notify_wechat.assert_not_called()
        self.assertEqual(self.signin.run_json.call_args.args[0][-1],'status')
    def test_early_warning_once_per_day(self):
        self.prime();r.tick(self.signin,self.now);r.tick(self.signin,self.now+timedelta(minutes=2))
        self.signin.notify_wechat.assert_called_once()
    def test_after_window_no_warning(self):
        self.prime();r.tick(self.signin,self.now.replace(hour=10));self.signin.notify_wechat.assert_not_called()
    def test_before_window_no_warning(self):
        self.prime();r.tick(self.signin,self.now.replace(minute=19));self.signin.notify_wechat.assert_not_called()
    def test_unknown_query_does_not_claim_offline(self):
        self.signin.run_json.side_effect=TimeoutError
        self.assertEqual(r.tick(self.signin,self.now)['state'],'unknown');self.signin.notify_wechat.assert_not_called()
    def test_reconnected_state_persisted_without_spam(self):
        self.prime();self.node['connected']=True
        self.assertEqual(r.tick(self.signin,self.now)['state'],'online');self.signin.notify_wechat.assert_not_called()
    def test_missing_capabilities_are_not_online(self):
        self.node.update(connected=True,commands=[])
        self.assertEqual(r.tick(self.signin,self.now)['state'],'capability_missing')
    def test_paused_schedule_not_warned(self):
        self.prime();self.job['enabled']=False;r.tick(self.signin,self.now);self.signin.notify_wechat.assert_not_called()
    def test_unrelated_job_not_routed(self):
        self.prime();self.job['payload']['argv'][0]='/bin/other';r.tick(self.signin,self.now)
        self.signin.notify_wechat.assert_not_called()
    def test_missing_notification_route_not_guessed(self):
        self.prime();self.job['payload']['argv']=[];r.tick(self.signin,self.now);self.signin.notify_wechat.assert_not_called()
    def test_timeout_after_send_never_tight_retried(self):
        self.prime();self.signin.notify_wechat.side_effect=TimeoutError
        r.tick(self.signin,self.now);r.tick(self.signin,self.now+timedelta(minutes=1));self.signin.notify_wechat.assert_called_once()
    def test_rejected_notice_keeps_result_without_false_success(self):
        self.prime();self.signin.notify_wechat.return_value={'sent':False,'reason':'wechat_upstream_prepare_rejected','queued':True}
        d=r.tick(self.signin,self.now);self.assertFalse(d['delivery']['sent']);self.assertTrue(d['delivery']['queued'])
    def test_new_day_can_warn_again(self):
        self.prime();r.tick(self.signin,self.now);r.tick(self.signin,self.now+timedelta(days=1))
        self.assertEqual(self.signin.notify_wechat.call_count,2)
    def test_private_state_has_no_message_token(self):
        self.prime();r.tick(self.signin,self.now)
        path=self.root/'android-readiness.json';self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertNotIn('recipient',path.read_text());self.assertNotIn('contextFingerprint',path.read_text())
    def test_windows_node_never_substitutes_phone(self):
        self.assertEqual(r.classify({'ok':True,'nodes':[{'displayName':'my-windows-pc','connected':True}]})[0],'unknown')
    def test_duplicate_phone_identity_is_unknown(self):
        self.assertEqual(r.classify({'ok':True,'nodes':[self.node,self.node]})[0],'unknown')
    def test_failed_status_payload_not_trusted(self):
        self.assertEqual(r.classify({'ok':False,'nodes':[self.node]})[0],'unknown')
    def test_unknown_query_does_not_erase_offline_interval(self):
        self.prime();original=self.signin.run_json.side_effect
        self.signin.run_json.side_effect=TimeoutError;r.tick(self.signin,self.now-timedelta(seconds=20))
        self.signin.run_json.side_effect=original;r.tick(self.signin,self.now)
        self.signin.notify_wechat.assert_called_once()
    def test_real_recovery_resets_old_offline_interval(self):
        self.prime();self.node['connected']=True;r.tick(self.signin,self.now-timedelta(seconds=20))
        self.node['connected']=False;r.tick(self.signin,self.now)
        self.signin.notify_wechat.assert_not_called()

class Terminal(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.addCleanup(patch.stopall);patch.object(o,'ROOT',self.root/'queue').start()
        context=self.root/'context';context.mkdir();patch.object(o,'CONTEXT',context).start()
        self.context=context/'bot.context-tokens.json';self.context.write_text('fake-old-context')
        self.time=datetime(2026,10,27,6,35,tzinfo=r.TZ).timestamp()
        self.rec={'createdAt':self.time,'message':'⚠ 06:35 尚未完成签到，补救中','attempts':1,'contextFingerprint':'fake'}
        self.run={'startedAt':'2026-10-27 06:28:00','endedAt':'2026-10-27 06:43:03','status':'failed',
                  'failedStage':{'stage':'node','nodeConnection':{'connected':False}}}
    def write_run(self):
        folder=self.root/'runs';folder.mkdir(exist_ok=True)
        (folder/'checkin-20261027-064303.json').write_text(json.dumps(self.run))
    def test_offline_final_replaces_progress(self):
        self.write_run();d=o.terminal_update(self.rec);self.assertIn('持续离线',d['message']);self.assertNotIn('补救中',d['message'])
    def test_success_replaces_prior_failure_warning(self):
        self.run['status']='passed';self.write_run();self.assertIn('已完成',o.terminal_update(self.rec)['message'])
    def test_pending_booking_is_urgent_not_generic_failure(self):
        self.run['status']='reservation_pending_checkin';self.write_run()
        self.assertIn('禁止重复选座',o.terminal_update(self.rec)['message'])
    def test_same_attempts_and_context_preserved(self):
        self.write_run();d=o.terminal_update(self.rec)
        self.assertEqual(d['attempts'],1);self.assertEqual(d['contextFingerprint'],'fake');self.assertEqual(d['createdAt'],self.time)
    def test_other_notification_not_rewritten(self):
        self.write_run();record={**self.rec,'message':'其他业务消息'};self.assertEqual(o.terminal_update(record),record)
    def test_future_or_other_run_not_rewritten(self):
        self.run['startedAt']='2026-10-27 07:00:00';self.run['endedAt']='2026-10-27 07:10:00'
        self.write_run();self.assertEqual(o.terminal_update(self.rec),self.rec)
    def test_missing_terminal_record_keeps_original(self):self.assertEqual(o.terminal_update(self.rec),self.rec)
    def test_malformed_terminal_record_keeps_original(self):
        self.run.pop('endedAt');self.write_run();self.assertEqual(o.terminal_update(self.rec),self.rec)
    def test_flush_updates_content_but_waits_for_fresh_context(self):
        self.write_run()
        with patch.object(o.time,'time',return_value=self.time+60):
            o.track({'sent':False,'reason':'wechat_upstream_prepare_rejected'},self.rec['message'],'weixin','bot','recipient')
            self.assertEqual(o.flush(lambda *a:self.fail('Must not send same context'))['waiting'],1)
            d=json.loads(next(o.ROOT.glob('*.json')).read_text());self.assertIn('持续离线',d['message'])
    def test_updated_final_sent_once_when_context_changes(self):
        self.write_run()
        with patch.object(o.time,'time',return_value=self.time+60):
            o.track({'sent':False,'reason':'wechat_upstream_prepare_rejected'},self.rec['message'],'weixin','bot','recipient')
            self.context.write_text('fake-fresh-context');send=Mock(return_value={'sent':True})
            self.assertEqual(o.flush(send)['sent'],1);o.flush(send);send.assert_called_once()
            self.assertIn('持续离线',send.call_args.args[0])

if __name__=='__main__':unittest.main()
