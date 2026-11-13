import importlib.util,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
s=importlib.util.spec_from_file_location('m','/tmp/android-signin-1022-reviewed.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class Tests(unittest.TestCase):
    def setUp(self):
        m.RUN_CONTEXT.clear();m.BUSINESS_DEADLINE=None
    def run_pair(self,fn):
        with patch.object(m,'vision',side_effect=fn):
            return m.parallel_visual_votes(b'fixed-image','image/jpeg',['a','b'])
    def test_two_successes_are_retained_without_retry(self):
        calls=[]
        def fn(*a):calls.append(a);return {'id':a[2]}
        r=self.run_pair(fn);self.assertEqual(r,[{'id':'a'},{'id':'b'}]);self.assertEqual(len(calls),2)
        self.assertEqual(calls[0][3],18)
    def test_only_failed_vote_is_retried(self):
        counts={'a':0,'b':0}
        def fn(raw,mime,prompt,timeout):
            counts[prompt]+=1
            if prompt=='a' and counts[prompt]==1:raise m.VisionProviderError('视觉模型请求失败：TimeoutError')
            return {'id':prompt}
        self.assertEqual(self.run_pair(fn),[{'id':'a'},{'id':'b'}]);self.assertEqual(counts,{'a':2,'b':1})
    def test_both_transient_failures_retry_once_each(self):
        counts={'a':0,'b':0}
        def fn(raw,mime,prompt,timeout):
            counts[prompt]+=1
            if counts[prompt]==1:raise m.VisionProviderError('URLError')
            return {'id':prompt}
        self.assertEqual(len(self.run_pair(fn)),2);self.assertEqual(counts,{'a':2,'b':2})
    def test_auth_error_is_not_retried(self):
        calls=[]
        def fn(*a):calls.append(a);raise m.VisionProviderError('视觉服务 HTTP 401：认证异常')
        with self.assertRaises(m.VisionProviderError):self.run_pair(fn)
        self.assertLessEqual(len(calls),2)
    def test_repeated_timeout_is_bounded(self):
        counts={'a':0,'b':0}
        def fn(raw,mime,prompt,timeout):
            counts[prompt]+=1
            if prompt=='a':raise m.VisionProviderError('TimeoutError')
            return {'id':prompt}
        with self.assertRaises(m.VisionProviderError):self.run_pair(fn)
        self.assertEqual(counts,{'a':2,'b':1})
    def test_expired_total_budget_does_not_start_retry(self):
        calls=[]
        def fn(*a):calls.append(a);raise m.VisionProviderError('TimeoutError')
        with patch.object(m.time,'monotonic',side_effect=[0,39]):
            with self.assertRaises(m.VisionProviderError):self.run_pair(fn)
        self.assertEqual(len(calls),2)
    def test_retries_keep_exact_same_image_and_independent_prompts(self):
        calls=[];counts={'a':0,'b':0}
        def fn(raw,mime,prompt,timeout):
            calls.append((raw,mime,prompt));counts[prompt]+=1
            if counts[prompt]==1:raise m.VisionProviderError('TimeoutError')
            return {'id':prompt}
        self.run_pair(fn);self.assertEqual(len(calls),4)
        self.assertTrue(all(raw==b'fixed-image' and mime=='image/jpeg' for raw,mime,prompt in calls))
    def target(self):
        return dict(target_found=True,confidence=.99,x_norm=500,y_norm=700,_visualSnapshotId='old')
    def test_double_tap_explicit_no_gesture_revalidates_once(self):
        with patch.object(m,'validate_visual_frame',side_effect=['one','two']) as verify,patch.object(m,'fast_coordinate',side_effect=[m.ActionNotSubmitted('stale'),{}]) as act:
            m.tap_visual(self.target(),945,2048,1440,3120,gesture='doubletap-bound')
            self.assertEqual(verify.call_count,2);self.assertEqual(act.call_count,2)
            self.assertEqual(act.call_args_list[1].args[:2],('doubletap-bound','two'))
    def test_partial_double_tap_never_replayed(self):
        with patch.object(m,'validate_visual_frame',return_value='one'),patch.object(m,'fast_coordinate',side_effect=m.FlowError('partial gesture')) as act:
            with self.assertRaises(m.FlowError):m.tap_visual(self.target(),945,2048,1440,3120,gesture='doubletap-bound')
            act.assert_called_once()
    def test_double_tap_rejections_do_not_loop(self):
        with patch.object(m,'validate_visual_frame',return_value='one'),patch.object(m,'fast_coordinate',side_effect=m.ActionNotSubmitted('stale')) as act:
            with self.assertRaises(m.ActionNotSubmitted):m.tap_visual(self.target(),945,2048,1440,3120,gesture='doubletap-bound')
            self.assertEqual(act.call_count,2)
    def test_other_visual_gestures_are_rejected(self):
        with patch.object(m,'fast_coordinate') as act:
            with self.assertRaises(m.FlowError):m.tap_visual(self.target(),945,2048,1440,3120,gesture='arbitrary')
            act.assert_not_called()
    def submission_error(self,error,old=None):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        with patch.object(m,'ROOT',root),patch.object(m,'tap_visual',side_effect=error),patch.object(m,'settle_pending') as settle:
            if old:m.save_pending(old)
            t=dict(target_found=True,confidence=.99,selection='primary',selected_seat='博学楼四楼半圆弧平7号')
            with self.assertRaises(type(error)):m.submit_and_settle((t,945,2048),root,1440,3120,None)
            current=m.pending_reservation();settle.assert_not_called()
            return current,list(root.glob('rejected-submission-*.json'))
    def test_vision_preflight_timeout_does_not_leave_fake_pending(self):
        current,archives=self.submission_error(m.VisionProviderError('TimeoutError'))
        self.assertIsNone(current);self.assertEqual(len(archives),1)
    def test_preflight_timeout_restores_previous_settled_receipt(self):
        old={'status':'settled','date':'2026-10-22','seat':'existing'}
        current,_=self.submission_error(m.VisionProviderError('TimeoutError'),old)
        self.assertEqual(current,old)
    def test_window_expiry_before_dispatch_does_not_leave_fake_pending(self):
        current,_=self.submission_error(m.WindowClosed('deadline'))
        self.assertIsNone(current)
    def test_unknown_dispatch_timeout_still_preserves_pending(self):
        current,archives=self.submission_error(m.FlowError('RPC timeout, outcome unknown'))
        self.assertEqual(current['status'],'submission_uncertain');self.assertEqual(archives,[])
    def test_cleanup_and_off_have_separate_deadline_reserves(self):
        deadlines=[]
        def record(*a,**k):deadlines.append((m.BUSINESS_DEADLINE-m.datetime.now(m.TZ)).total_seconds());return {}
        with patch.object(m,'clear_recent_apps',side_effect=record),patch.object(m,'turn_screen_off',side_effect=record):
            m.finish_with_budget(Path('/tmp/unused-no-write'),1440,3120)
        self.assertTrue(34<deadlines[0]<=35);self.assertTrue(74<deadlines[1]<=75)
        self.assertGreater(deadlines[1]-deadlines[0],39)
    def test_finish_budget_restores_prior_business_deadline(self):
        prior=m.datetime.now(m.TZ);m.BUSINESS_DEADLINE=prior
        with patch.object(m,'best_effort_finish',return_value={}):m.finish_with_budget(Path('/tmp/unused-no-write'))
        self.assertEqual(m.BUSINESS_DEADLINE,prior)
    def test_cleanup_failure_does_not_skip_screen_off(self):
        with patch.object(m,'clear_recent_apps',side_effect=m.FlowError('stale')),patch.object(m,'turn_screen_off',return_value={'requested':True}) as off:
            result=m.finish_with_budget(Path('/tmp/unused-no-write'),1440,3120)
        off.assert_called_once();self.assertIn('cleanupError',result);self.assertTrue(result['screenOff']['requested'])
if __name__=='__main__':unittest.main()
