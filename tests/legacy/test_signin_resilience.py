import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from datetime import timedelta

spec = importlib.util.spec_from_file_location('signin', str(Path(__file__).with_name('android-signin-audit.py')))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def observation(found=False, success=False, **kw):
    return dict(target_found=found, success=success, confidence=.99, x_norm=500,
                y_norm=577, page='首页', evidence='页面正常', **kw)


class Resilience(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        m.BOOKING_OPENS = None
        m.BUSINESS_DEADLINE = None
        m.SETTLEMENT_DEADLINE = None
        m.RUN_CONTEXT.clear()
        self.sleep = patch.object(m.time, 'sleep').start()
        self.addCleanup(patch.stopall)
        patch.object(m, 'ROOT', self.root).start()
        patch.object(m, 'final_checkin_once', return_value={'success': False}).start()
        patch.object(m, 'model_preflight', return_value=None).start()
        patch.object(m, 'fast_coordinate', side_effect=AssertionError('unexpected real action')).start()
        patch.object(m, 'invoke', side_effect=AssertionError('unexpected real RPC')).start()
        patch.object(m, 'provider', side_effect=AssertionError('unexpected provider')).start()

    def popup(self, items):
        with patch.object(m, 'inspect', side_effect=[(x,945,2048) for x in items]) as reads, patch.object(m,'tap_visual') as taps:
            result = m.dismiss_school_binding_popup(self.root,'test',1440,3120)
            return result, reads.call_count, taps.call_count

    def test_popup_refresh(self):
        result,reads,taps=self.popup([observation(True),observation(success=True)])
        self.assertTrue(result['handled']); self.assertEqual((reads,taps),(2,1))

    def test_third_confirmation_can_succeed(self):
        result,reads,taps=self.popup([observation(True)]*3+[observation(success=True)])
        self.assertTrue(result['handled']); self.assertEqual((reads,taps),(4,3))

    def test_persistent_popup_bounded(self):
        with patch.object(m,'inspect',return_value=(observation(True),945,2048)),patch.object(m,'tap_visual') as taps:
            with self.assertRaises(m.FlowError): m.dismiss_school_binding_popup(self.root,'test',1440,3120)
            self.assertEqual(taps.call_count,3)

    def test_unrelated_popup_no_click(self):
        self.assertEqual(self.popup([observation(success=True)])[2],0)

    def test_consensus_disagreement_no_success(self):
        self.assertFalse(m.merge_visual_votes(observation(success=True),observation())['success'])

    def test_seat_votes_disagree_no_click(self):
        a=observation(True); a['selection']='primary'
        b=observation(True); b['selection']='backup'
        result=m.merge_visual_votes(a,b)
        self.assertFalse(result['target_found']); self.assertFalse(result['success'])

    def test_seat_votes_agree_backup(self):
        a=observation(True); a['selection']='backup'
        self.assertTrue(m.merge_visual_votes(a,a)['target_found'])

    def test_owned_seat_not_new_click(self):
        a=observation(success=True); a['selection']='owned'
        result=m.merge_visual_votes(a,a)
        self.assertTrue(result['success']); self.assertFalse(result['target_found'])

    def test_second_vote_lock_is_preserved(self):
        self.assertTrue(m.merge_visual_votes(observation(),observation(secure_lock=True))['secure_lock'])

    def test_coordinate_disagreement_no_click(self):
        other=observation(True); other['x_norm']=950
        self.assertFalse(m.merge_visual_votes(observation(True),other)['target_found'])

    def test_slow_loading_reobserved(self):
        with patch.object(m,'recover_page',return_value={'action':'wait'}), patch.object(m,'inspect',side_effect=[(observation(loading=True),945,2048)]*2+[(observation(True),945,2048)]) as reads:
            self.assertTrue(m.wait_target('target',self.root,'slow',1440,3120,checks=3)[0]['target_found'])
            self.assertEqual(reads.call_count,3)

    def test_missing_element_bounded(self):
        with patch.object(m,'recover_page',return_value={'action':'wait'}), patch.object(m,'inspect',return_value=(observation(),945,2048)) as reads:
            with self.assertRaises(m.FlowError): m.wait_target('target',self.root,'missing',1440,3120,checks=3)
            self.assertEqual(reads.call_count,3)

    def test_expired_window_no_observe_or_action(self):
        with patch.object(m,'inspect') as reads:
            with self.assertRaises(m.WindowClosed):
                m.wait_target('target',self.root,'expired',1440,3120,deadline=m.datetime.now(m.TZ)-timedelta(seconds=1))
            reads.assert_not_called()

    def checkin(self, responses, force=False):
        creds=self.root/'creds.json'; creds.write_text(json.dumps(dict(username='TEST',password='TEST')))
        with patch.object(m,'WEGOLIBRARY_CREDENTIALS',creds), patch.object(m,'_wegolibrary_request',side_effect=responses) as request:
            result=m.complete_wegolibrary_checkin(force=force)
            return result,request.call_count

    def test_backend_failure_not_success(self):
        with self.assertRaises(m.WegoLibraryError): self.checkin([{'access_token':'TEST'},dict(is_configured=True,wechat_connection_status='connected'),{'success':False,'message':'没有选座'}])

    def test_old_status_cannot_confirm_new_checkin(self):
        with self.assertRaises(m.WegoLibraryError): self.checkin([{'access_token':'TEST'},dict(is_configured=True,wechat_connection_status='connected'),{'success':True},dict(last_checkin='2000-01-01',last_checkin_result='签到成功')])

    def test_today_idempotent_does_not_post_checkin(self):
        result,calls=self.checkin([{'access_token':'TEST'},dict(is_configured=True,wechat_connection_status='connected',last_checkin=m.now_text(),last_checkin_result='签到成功')])
        self.assertTrue(result['idempotent']); self.assertEqual(calls,2)

    def test_new_booking_today_forces_fresh_checkin(self):
        earlier=(m.datetime.now(m.TZ)-timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')
        result,calls=self.checkin([{'access_token':'TEST'},dict(is_configured=True,wechat_connection_status='connected',last_checkin=earlier,last_checkin_result='签到成功'),{'success':True},dict(last_checkin=m.now_text(),last_checkin_result='签到成功')],force=True)
        self.assertTrue(result['success']);self.assertEqual(calls,4)

    def test_full_transaction_retried_after_backend_failure(self):
        with patch.object(m,'ROOT',self.root),patch.object(m.sys,'argv',['signin','--max-attempts','2']),patch.object(m,'ensure_node'),patch.object(m,'run_once',side_effect=[m.WegoLibraryError('backend unavailable'),dict(result={'success':True},wegolibrary={'success':True})]) as run,patch.object(m,'best_effort_finish',return_value={}),patch.object(m,'write_run'),patch('builtins.print'):
            self.assertEqual(m.main(),0); self.assertEqual(run.call_count,2)

    def test_node_reconnection_retries(self):
        with patch.object(m,'ROOT',self.root),patch.object(m.sys,'argv',['signin','--max-attempts','2']),patch.object(m,'ensure_node',side_effect=[m.FlowError('Android Node 离线'),None]),patch.object(m,'run_once',return_value={}) as run,patch.object(m,'best_effort_finish',return_value={}),patch.object(m,'write_run'),patch('builtins.print'):
            self.assertEqual(m.main(),0); self.assertEqual(run.call_count,1)

    def test_recovery_wait_captures_before_after(self):
        with patch.object(m,'screenshot',return_value=(b'TEST','image/jpeg',945,2048)) as shots,patch.object(m,'recovery_plan',return_value={'action':'wait','confidence':.99}):
            result=m.recover_page('seat','loading',self.root,'recover',1440,3120,[])
            self.assertEqual(result['action'],'wait'); self.assertEqual(shots.call_count,2)

    def test_recovery_repeated_action_escalates_restart(self):
        with patch.object(m,'screenshot',return_value=(b'TEST','image/jpeg',945,2048)),patch.object(m,'recovery_plan',return_value={'action':'wait','confidence':.99}):
            with self.assertRaises(m.RestartFlow): m.recover_page('seat','loading',self.root,'repeat',1440,3120,[{'action':'wait'}])

    def test_recovery_arbitrary_model_command_rejected(self):
        with patch.object(m,'screenshot',return_value=(b'TEST','image/jpeg',945,2048)),patch.object(m,'recovery_plan',return_value={'action':'execute_shell','confidence':.99}):
            with self.assertRaises(m.RestartFlow): m.recover_page('seat','error',self.root,'invalid',1440,3120,[])

    def test_recovery_model_failure_escalates_restart(self):
        with patch.object(m,'screenshot',return_value=(b'TEST','image/jpeg',945,2048)),patch.object(m,'recovery_plan',side_effect=TimeoutError()):
            with self.assertRaises(m.RestartFlow): m.recover_page('seat','error',self.root,'timeout',1440,3120,[])

    def test_recovery_expired_does_not_call_model(self):
        with patch.object(m,'recovery_plan') as model:
            with self.assertRaises(m.WindowClosed): m.recover_page('seat','error',self.root,'expired',1440,3120,[],m.datetime.now(m.TZ)-timedelta(seconds=1))
            model.assert_not_called()

    def test_recovery_lock_stops(self):
        with patch.object(m,'screenshot',return_value=(b'TEST','image/jpeg',945,2048)),patch.object(m,'recovery_plan',return_value={'action':'back','confidence':.99,'secure_lock':True}):
            with self.assertRaisesRegex(m.FlowError,'人工解锁'): m.recover_page('seat','error',self.root,'locked',1440,3120,[])

    def test_normalized_click_scale_variants(self):
        for width,height in [(720,1560),(1080,2340),(1440,3120)]:
            with patch.object(m,'fast_coordinate') as click,patch.object(m,'validate_visual_frame',return_value='test-frame'):
                m.tap_visual(observation(True,_visualSnapshotId='test-frame'),945,2048,width,height)
                self.assertEqual(click.call_args.args,('tap-bound','test-frame',round(width*.5),round(height*.577)))

    def test_rotation_mismatch_blocks_click(self):
        with patch.object(m,'fast_coordinate') as click:
            with self.assertRaises(m.FlowError): m.tap_visual(observation(True),2048,945,1440,3120)
            click.assert_not_called()

    def test_bad_coordinates_blocked(self):
        for invalid in [float('nan'),float('inf'),-1,0,1001]:
            item=observation(True);item['x_norm']=invalid
            with self.assertRaises(m.FlowError):m.tap_visual(item,945,2048,1440,3120)

    def test_no_click_after_deadline(self):
        m.BUSINESS_DEADLINE=m.datetime.now(m.TZ)-timedelta(seconds=1)
        with self.assertRaises(m.WindowClosed):m.tap_visual(observation(True),945,2048,1440,3120)

    def test_wait_until_opening_then_fresh_selection(self):
        start=m.datetime.now(m.TZ)
        m.BOOKING_OPENS=start+timedelta(seconds=20)
        with patch.object(m,'datetime') as clock:
            clock.now.side_effect=[start,start,start+timedelta(seconds=20)]
            m.wait_for_opening()
        self.sleep.assert_called_with(10.0)

    def test_expired_window_blocks_opening_wait(self):
        m.BOOKING_OPENS=m.datetime.now(m.TZ)+timedelta(minutes=1)
        with self.assertRaises(m.WindowClosed):m.wait_for_opening(m.datetime.now(m.TZ)-timedelta(seconds=1))

    def test_lock_excludes_duplicate_and_releases(self):
        with patch.object(m, 'ROOT', self.root):
            with m.single_run():
                with self.assertRaises(m.AlreadyRunning):
                    with m.single_run(): pass
            with m.single_run(): pass

    def test_stage_persisted(self):
        m.mark_stage(self.root, 'checkin', 'verify result')
        data=json.loads((self.root/'progress.json').read_text())
        self.assertEqual(data['stage'], 'checkin')
        self.assertFalse((self.root/'progress.tmp').exists())

    def test_timeout_capped_to_remaining_window(self):
        m.BUSINESS_DEADLINE=m.datetime.now(m.TZ)+timedelta(seconds=3)
        self.assertGreater(m.bounded_timeout(90),0)
        self.assertLessEqual(m.bounded_timeout(90),3)

    def test_expired_window_blocks_subprocess(self):
        m.BUSINESS_DEADLINE=m.datetime.now(m.TZ)-timedelta(seconds=1)
        with patch.object(m.subprocess,'run') as run:
            with self.assertRaises(m.WindowClosed): m.run_json(['unused'])
            run.assert_not_called()

    def test_success_record_failure_never_rebooks(self):
        with patch.object(m,'ROOT',self.root),patch.object(m.sys,'argv',['signin','--max-attempts','2']),patch.object(m,'ensure_node'),patch.object(m,'run_once',return_value={}) as run,patch.object(m,'write_run',side_effect=OSError('disk full')),patch('builtins.print'):
            self.assertEqual(m.main(),0)
            self.assertEqual(run.call_count,1)

    def test_success_finish_failure_never_rebooks(self):
        with patch.object(m,'ROOT',self.root),patch.object(m.sys,'argv',['signin','--max-attempts','2','--finish-phone']),patch.object(m,'ensure_node'),patch.object(m,'run_once',return_value={}) as run,patch.object(m,'best_effort_finish',side_effect=RuntimeError('node gone')),patch.object(m,'write_run') as record,patch('builtins.print'):
            self.assertEqual(m.main(),0)
            self.assertEqual(run.call_count,1)
            self.assertEqual(record.call_args.args[0]['finish']['cleanupError'],'RuntimeError')

    def test_expired_window_blocks_backend_read(self):
        m.BUSINESS_DEADLINE=m.datetime.now(m.TZ)-timedelta(seconds=1)
        with patch.object(m.urllib.request,'urlopen') as request:
            with self.assertRaises(m.WindowClosed): m._wegolibrary_request('/api/status')
            request.assert_not_called()

    def test_known_popup_overrides_lock_misclassification(self):
        result=observation(False,secure_lock=True,school_binding_popup=True)
        with patch.object(m,'screenshot',return_value=(b'fake','image/jpeg',945,2048)),patch.object(m,'vision',return_value=result):
            got,_,_=m.inspect('verify',self.root,'popup')
        self.assertFalse(got['secure_lock']);self.assertFalse(got['success'])

    def test_negative_popup_evidence_not_matched(self):
        self.assertFalse(m.school_binding_popup_visible({'evidence':'未出现您的学校需要您绑定账户'}))

    def test_negative_login_popup_description_1008(self):
        for evidence in ('页面加载完整，无未登录、学校绑定或系统公告等阻断弹窗；未指定具体目标，故无需要确认的操作',
                         '未发现登录失效、学校绑定或公告等拦截弹窗，无需要确认的操作'):
            self.assertFalse(m.school_binding_popup_visible({'page':'首页','evidence':evidence}))
            self.assertFalse(m.school_binding_popup_visible({'page':'首页','evidence':evidence,
                'transient_auth_popup':False,'school_binding_popup':False,'dialog_text':''}))
        self.assertTrue(m.school_binding_popup_visible({'transient_auth_popup':True,
            'dialog_text':'您尚未登录','confirm_text':'确定'}))

    def test_black_screen_manual_plan_restarts(self):
        plan={'action':'manual','confidence':.99,'reason':'设备黑屏需要唤醒','secure_lock':False}
        with patch.object(m,'screenshot',return_value=(b'fake','image/jpeg',945,2048)),patch.object(m,'recovery_plan',return_value=plan):
            with self.assertRaises(m.RestartFlow): m.recover_page('conversation','error',self.root,'black',1440,3120,[])

    def test_notification_timeout_is_not_business_failure(self):
        with patch.object(m.subprocess,'run',side_effect=m.subprocess.TimeoutExpired('mock',45)):
            self.assertFalse(m.notify_wechat('test','test','test','test')['sent'])

    def test_second_vote_popup_cannot_be_lost(self):
        a=observation(True)
        b=observation(True,school_binding_popup=True)
        b['evidence']='提示弹窗'
        result=m.merge_visual_votes(a,b)
        self.assertTrue(m.school_binding_popup_visible(result))
        self.assertFalse(result['target_found'])

    def test_string_false_not_clickable(self):
        item=observation(True);item['target_found']='false'
        with patch.object(m,'fast_coordinate') as click:
            with self.assertRaises(m.FlowError):m.tap_visual(item,945,2048,1440,3120)
            click.assert_not_called()

    def test_string_false_backend_not_success(self):
        with self.assertRaises(m.WegoLibraryError):
            self.checkin([{'access_token':'TEST'},dict(is_configured=True,wechat_connection_status='connected'),{'success':'false'},dict(last_checkin=m.now_text(),last_checkin_result='签到成功')])

    def test_negative_success_text_not_cached(self):
        with self.assertRaises(m.WegoLibraryError):
            self.checkin([{'access_token':'TEST'},dict(is_configured=True,wechat_connection_status='connected',last_checkin=m.now_text(),last_checkin_result='未签到成功'),{'success':False,'message':'失败'}])

    def test_popup_without_target_is_not_disappearance(self):
        with patch.object(m,'inspect',return_value=(observation(),945,2048)) as reads:
            with self.assertRaises(m.FlowError):m.dismiss_school_binding_popup(self.root,'uncertain',1440,3120)
            self.assertEqual(reads.call_count,8)

    def test_0913_occupied_seats_manual_must_restart(self):
        plan={'action':'manual','confidence':.99,'reason':'主座位和备用均已被预订，需要人工介入或另选座位','secure_lock':False}
        with patch.object(m,'screenshot',return_value=(b'fake','image/jpeg',945,2048)),patch.object(m,'recovery_plan',return_value=plan):
            with self.assertRaises(m.RestartFlow):m.recover_page('seat','均已被预订',self.root,'occupied',1440,3120,[])

    def test_real_login_requires_visible_evidence(self):
        plan={'action':'manual','confidence':.99,'manual_kind':'login','auth_evidence':'登录表单包含密码输入框','secure_lock':False}
        with patch.object(m,'screenshot',return_value=(b'fake','image/jpeg',945,2048)),patch.object(m,'recovery_plan',return_value=plan):
            with self.assertRaisesRegex(m.FlowError,'实际登录页'):m.recover_page('seat','login',self.root,'login',1440,3120,[])

    def test_occupied_restart_continues_transaction(self):
        with patch.object(m,'ROOT',self.root),patch.object(m.sys,'argv',['signin','--max-attempts','3']),patch.object(m,'ensure_node'),patch.object(m,'run_once',side_effect=[m.RestartFlow('两座位被占'),m.RestartFlow('两座位被占'),{}]) as run,patch.object(m,'best_effort_finish',return_value={}),patch.object(m,'write_run'),patch('builtins.print'):
            self.assertEqual(m.main(),0);self.assertEqual(run.call_count,3)

    def test_transient_login_word_variants(self):
        for text in ('尚未登录','尚未登陆','请先登录','请先登陆','登录过期'):
            with self.subTest(text=text):
                self.assertTrue(m.school_binding_popup_visible({'page':'提示弹窗','evidence':text+'，点击确认'}))

    def test_login_confirmation_waits_through_refresh(self):
        popup=observation(True,transient_auth_popup=True)
        loading=observation(success=True,loading=True)
        result,reads,taps=self.popup([popup,loading,observation(loading=True),observation(success=True)])
        self.assertTrue(result['handled']);self.assertEqual((reads,taps),(4,1))

    def test_login_popup_reappears_after_refresh(self):
        popup=observation(True,transient_auth_popup=True)
        result,reads,taps=self.popup([popup,observation(loading=True),popup,observation(success=True)])
        self.assertTrue(result['handled']);self.assertEqual((reads,taps),(4,2))

    def test_password_form_is_not_confirmation(self):
        self.assertFalse(m.school_binding_popup_visible({'page':'登录表单','evidence':'请先登录，需要输入账号和密码'}))

    def test_login_popup_overrides_manual_recovery(self):
        plan={'action':'manual','confidence':.99,'transient_auth_popup':True,'reason':'尚未登陆，确认即可'}
        with patch.object(m,'screenshot',return_value=(b'fake','image/jpeg',945,2048)),patch.object(m,'recovery_plan',return_value=plan),patch.object(m,'dismiss_school_binding_popup',return_value={'handled':True}) as confirm:
            result=m.recover_page('seat','尚未登陆',self.root,'login-confirm',1440,3120,[])
            self.assertEqual(result['action'],'confirm_binding');confirm.assert_called_once()

    def test_offline_node_retains_connection_evidence(self):
        with patch.object(m,'run_json',return_value={'nodes':[{'displayName':m.NODE,'connected':False,'lastSeenAtMs':123,'approvalState':'approved'}]}):
            with self.assertRaises(m.FlowError):m.ensure_node()
        self.assertEqual(m.RUN_CONTEXT['nodeConnection']['lastSeenAtMs'],123)
        self.assertFalse(m.RUN_CONTEXT['nodeConnection']['connected'])

    def test_offline_notification_failure_still_reconnects(self):
        with patch.object(m,'ROOT',self.root),patch.object(m.sys,'argv',['signin','--max-attempts','2']),patch.object(m,'ensure_node',side_effect=[m.FlowError('Android Node 离线'),None]),patch.object(m,'run_once',return_value={}),patch.object(m,'write_run'),patch.object(m,'notify_wechat',return_value={'sent':False,'reason':'timeout'}),patch('builtins.print'):
            self.assertEqual(m.main(),0)
        self.sleep.assert_any_call(10.0)
        records=list(self.root.rglob('node-offline-notify.json'))
        self.assertEqual(len(records),1)
        self.assertFalse(json.loads(records[0].read_text())['sent'])

    def test_late_submission_never_clicks(self):
        with patch.object(m,'ROOT',self.root),patch.object(m,'tap_visual') as click:
            with self.assertRaises(m.WindowClosed):m.submit_and_settle((observation(True),945,2048),self.root,1440,3120,m.datetime.now(m.TZ)+timedelta(seconds=5))
            click.assert_not_called()

    def test_submit_persists_before_click_and_signs_without_vision(self):
        def clicked(*args):self.assertEqual(m.pending_reservation()['status'],'submission_uncertain')
        with patch.object(m,'ROOT',self.root),patch.object(m,'tap_visual',side_effect=clicked),patch.object(m,'complete_wegolibrary_checkin',return_value={'success':True}) as api,patch.object(m,'inspect',side_effect=AssertionError('No vision before checkin')):
            m.submit_and_settle((observation(True,selected_seat='博学楼四楼半圆弧平7号',selection='primary'),945,2048),self.root,1440,3120,None)
            api.assert_called_once_with(force=True)
            self.assertEqual(m.pending_reservation()['status'],'settled')

    def test_rpc_timeout_preserves_pending(self):
        with patch.object(m,'ROOT',self.root),patch.object(m,'tap_visual',side_effect=TimeoutError()):
            with self.assertRaises(TimeoutError):m.submit_and_settle((observation(True,selected_seat='博学楼四楼半圆弧平7号',selection='primary'),945,2048),self.root,1440,3120,None)
            self.assertEqual(m.pending_reservation()['status'],'submission_uncertain')

    def test_pending_recovery_does_not_require_phone_or_rebook(self):
        with patch.object(m,'ROOT',self.root),patch.object(m.sys,'argv',['signin']),patch.object(m,'ensure_node',side_effect=AssertionError('Phone not required')),patch.object(m,'run_once',side_effect=AssertionError('No rebooking')),patch.object(m,'complete_wegolibrary_checkin',return_value={'success':True}),patch('builtins.print'):
            m.save_pending({'status':'submission_uncertain','date':m.datetime.now(m.TZ).strftime('%Y-%m-%d')})
            self.assertEqual(m.main(),0)

    def test_safety_hold_blocks_all_business(self):
        (self.root/'SAFETY_HOLD').write_text('hold')
        with patch.object(m,'ROOT',self.root),patch.object(m,'ensure_node') as node,patch('builtins.print'):
            self.assertEqual(m.main(),1);node.assert_not_called()

    def test_pending_can_settle_after_booking_cutoff(self):
        m.BUSINESS_DEADLINE=m.datetime.now(m.TZ)-timedelta(seconds=1)
        m.SETTLEMENT_DEADLINE=m.datetime.now(m.TZ)+timedelta(minutes=7)
        with patch.object(m,'ROOT',self.root),patch.object(m,'complete_wegolibrary_checkin',return_value={'success':True}) as api:
            m.save_pending({'status':'submission_uncertain','date':m.datetime.now(m.TZ).strftime('%Y-%m-%d')})
            m.settle_pending(self.root);api.assert_called_once()

    def test_no_new_booking_after_cutoff_even_with_settlement_time(self):
        m.SETTLEMENT_DEADLINE=m.datetime.now(m.TZ)+timedelta(minutes=7)
        with patch.object(m,'tap_visual') as tap:
            with self.assertRaises(m.WindowClosed):m.submit_and_settle((observation(True),945,2048),self.root,1440,3120,m.datetime.now(m.TZ)-timedelta(seconds=1))
            tap.assert_not_called()

    def test_settlement_cutoff_blocks_api(self):
        m.SETTLEMENT_DEADLINE=m.datetime.now(m.TZ)-timedelta(seconds=1)
        with patch.object(m,'complete_wegolibrary_checkin') as api:
            with self.assertRaises(m.WindowClosed):m.settle_pending(self.root)
            api.assert_not_called()

if __name__=='__main__': unittest.main(verbosity=2)
