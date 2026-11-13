import importlib.util, tempfile, unittest, json, subprocess
from pathlib import Path
from unittest.mock import patch
s=importlib.util.spec_from_file_location('m','/tmp/android-signin-1022-reviewed.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
real_fast=m.fast_coordinate
def entry(route='library_webview',**kw):
    return ({'entry_route':route,'target_found':False,'confidence':.99,'page':route,**kw},945,2048)
class Tests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.addCleanup(patch.stopall);patch.object(m,'ROOT',self.root).start()
        m.RUN_CONTEXT.clear();m.BUSINESS_DEADLINE=m.BOOKING_OPENS=m.SETTLEMENT_DEADLINE=None
        self.sleep=patch.object(m.time,'sleep').start()
        for name in ('invoke','provider','fast_coordinate'):
            patch.object(m,name,side_effect=AssertionError('No external call: '+name)).start()
        patch.object(m,'prepare_display',return_value=(1440,3120)).start()
        self.observe=patch.object(m,'observe',return_value={'snapshotId':'s'}).start()
        self.act=patch.object(m,'act').start()
        self.refresh=patch.object(m,'refresh_seat_page').start()
        self.cold=patch.object(m,'open_seat_page_from_conversation').start()
        self.opening=patch.object(m,'wait_and_refresh_opening').start()
        self.popup=patch.object(m,'dismiss_school_binding_popup',return_value={'handled':True}).start()
        self.wait=patch.object(m,'wait_target',return_value=({'selection':'primary','target_found':True},945,2048)).start()
        self.submit=patch.object(m,'submit_and_settle',return_value={'success':True}).start()
    def run_entry(self,items):
        with patch.object(m,'inspect',side_effect=items):return m.run_once(self.root)
    def test_existing_h5_reopens_without_backtracking_chat_list(self):
        self.assertTrue(self.run_entry([entry()])['success'])
        self.refresh.assert_called_once();self.cold.assert_not_called();self.act.assert_not_called()
        self.opening.assert_called_once();self.submit.assert_called_once()
    def test_official_chat_reuses_exact_menu_route(self):
        self.run_entry([entry('official_conversation')]);self.refresh.assert_called_once();self.cold.assert_not_called()
    def test_unknown_route_keeps_original_safe_path(self):
        self.run_entry([entry('unknown'),entry('conversation_list',target_found=True)])
        self.refresh.assert_not_called();self.cold.assert_called_once();self.act.assert_called_once()
    def test_loading_never_reuses_entry(self):
        self.run_entry([entry(loading=True),entry('conversation_list',target_found=True)])
        self.refresh.assert_not_called();self.act.assert_called_once()
    def test_popup_dismissed_before_reusing_page(self):
        self.run_entry([entry(transient_auth_popup=True),entry()])
        self.popup.assert_called_once();self.refresh.assert_called_once();self.act.assert_not_called()
    def test_original_cold_chat_list_path_retained(self):
        self.run_entry([entry('conversation_list',target_found=True)])
        self.cold.assert_called_once();self.refresh.assert_not_called()
    def test_other_foreground_app_is_not_backed_or_swiped(self):
        def inspect(*a,**k):
            if not getattr(inspect,'called',False):
                inspect.called=True;m.RUN_CONTEXT['visualFrame']={'package':'other.app'}
                return entry('other')
            m.RUN_CONTEXT['visualFrame']={'package':m.WECHAT}
            return entry('conversation_list',target_found=True)
        with patch.object(m,'inspect',side_effect=inspect),patch.object(m,'invoke') as launch:
            m.run_once(self.root);launch.assert_called_once_with('mobile.ui.launch',{'package':m.WECHAT})
        self.act.assert_not_called()
    def test_owned_reservation_goes_to_settlement_not_new_submission(self):
        self.wait.return_value=({'success':True,'target_found':False,'selected_seat':'博学楼四楼半圆弧平7号'},945,2048)
        with patch.object(m,'settle_pending',return_value={}) as settle:
            self.run_entry([entry()]);settle.assert_called_once();self.submit.assert_not_called()
            self.assertEqual(m.pending_reservation()['status'],'owned_unconfirmed')
    def test_refresh_error_never_submits(self):
        self.refresh.side_effect=m.RestartFlow('route changed')
        with self.assertRaises(m.RestartFlow):self.run_entry([entry()])
        self.submit.assert_not_called()
    def test_route_votes_must_agree(self):
        def shot():m.RUN_CONTEXT['visualFrame']={'snapshotId':'s'};return b'pixels','image/jpeg',945,2048
        with patch.object(m,'screenshot',side_effect=shot),patch.object(m,'vision',side_effect=[entry()[0],entry('other')[0]]):
            r,_,_=m.inspect('entry',self.root,'entry',consensus=True,consensus_fields=('entry_route',))
        self.assertEqual(r['entry_route'],'unknown')
    def test_low_confidence_route_is_not_trusted(self):
        def shot():m.RUN_CONTEXT['visualFrame']={'snapshotId':'s'};return b'pixels','image/jpeg',945,2048
        with patch.object(m,'screenshot',side_effect=shot),patch.object(m,'vision',return_value=entry(confidence=.8)[0]):
            r,_,_=m.inspect('entry',self.root,'entry',consensus=True,consensus_fields=('entry_route',))
        self.assertEqual(r['entry_route'],'unknown')
    def test_partial_double_tap_is_not_reported_as_no_gesture(self):
        payload={'ok':False,'result':{'code':'target_stale'},'completedGestures':1}
        with patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],1,json.dumps(payload),'')):
            with self.assertRaises(m.FlowError) as caught:real_fast('doubletap-bound','s',100,100)
        self.assertNotIsInstance(caught.exception,m.ActionNotSubmitted)
    def test_first_rejected_tap_still_safe_to_reobserve(self):
        payload={'ok':False,'result':{'code':'target_stale'},'completedGestures':0}
        with patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],1,json.dumps(payload),'')):
            with self.assertRaises(m.ActionNotSubmitted):real_fast('tap-bound','s',100,100)
    def test_secure_lock_stops_launch_retry(self):
        patch.stopall()
        with patch.object(m.time,'sleep'),patch.object(m,'fast_coordinate'),patch.object(m,'screenshot',return_value=(b'pixels','image/jpeg',945,2048)),patch.object(m,'parallel_visual_votes',return_value=[{'secure_lock':True,'confidence':.99}]*2),patch.object(m,'invoke',side_effect=m.FlowError('DEVICE_LOCKED')) as launch:
            with self.assertRaisesRegex(m.FlowError,'手机需要人工解锁'):m.prepare_display()
            self.assertEqual(launch.call_count,2)
    def test_device_locked_code_on_recents_is_not_called_secure_lock(self):
        patch.stopall()
        with patch.object(m.time,'sleep'),patch.object(m,'fast_coordinate'),patch.object(m,'screenshot',return_value=(b'pixels','image/jpeg',945,2048)),patch.object(m,'observe',return_value={'package':'com.sec.android.app.launcher'}),patch.object(m,'parallel_visual_votes',return_value=[{'secure_lock':False,'confidence':.99}]*2),patch.object(m,'invoke',side_effect=m.FlowError('DEVICE_LOCKED')):
            with self.assertRaises(m.FlowError) as error:m.prepare_display()
            self.assertNotIn('人工解锁',str(error.exception))
    def test_transient_lock_code_can_recover_on_second_launch(self):
        patch.stopall()
        with patch.object(m.time,'sleep'),patch.object(m,'fast_coordinate') as action,patch.object(m,'screenshot',return_value=(b'pixels','image/jpeg',945,2048)),patch.object(m,'observe',return_value={'package':m.WECHAT}),patch.object(m,'full_display_bounds',return_value=(1440,3120)),patch.object(m,'invoke',side_effect=[m.FlowError('DEVICE_LOCKED'),{}]) as launch,patch.object(m,'parallel_visual_votes') as votes:
            self.assertEqual(m.prepare_display(),(1440,3120));self.assertEqual(launch.call_count,2)
            votes.assert_not_called();self.assertIn(('global','home'),[c.args for c in action.call_args_list])
    def test_failed_launch_can_only_resume_if_actual_wechat_is_visible(self):
        patch.stopall()
        with patch.object(m.time,'sleep'),patch.object(m,'fast_coordinate'),patch.object(m,'screenshot',return_value=(b'pixels','image/jpeg',945,2048)),patch.object(m,'observe',return_value={'package':m.WECHAT}),patch.object(m,'full_display_bounds',return_value=(1440,3120)),patch.object(m,'parallel_visual_votes',return_value=[{'secure_lock':False,'confidence':.99}]*2),patch.object(m,'invoke',side_effect=m.FlowError('DEVICE_LOCKED')):
            self.assertEqual(m.prepare_display(),(1440,3120))
    def test_closed_window_is_not_wrapped_as_launch_error(self):
        patch.stopall()
        with patch.object(m.time,'sleep'),patch.object(m,'fast_coordinate'),patch.object(m,'invoke',side_effect=m.WindowClosed('cutoff')) as launch:
            with self.assertRaises(m.WindowClosed):m.prepare_display()
            launch.assert_called_once()
if __name__=='__main__':unittest.main()
