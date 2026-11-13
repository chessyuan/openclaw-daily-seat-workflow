import importlib.util,json,tempfile,unittest,subprocess,base64,io
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
s=importlib.util.spec_from_file_location('m','/tmp/android-signin-1021-final.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
real_fast_coordinate=m.fast_coordinate

def target(**kw):
    result=dict(target_found=True,success=False,confidence=.99,x_norm=500,y_norm=500,
        selection='primary',selected_seat='博学楼四楼半圆弧平7号',_visualSnapshotId='frame-old')
    result.update(kw);return result

class Tests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.addCleanup(patch.stopall)
        patch.object(m,'ROOT',self.root).start();patch.object(m.time,'sleep').start()
        for name in ('invoke','provider','fast_coordinate','run_json'):
            patch.object(m,name,side_effect=AssertionError('No outbound calls: '+name)).start()
        m.RUN_CONTEXT.clear();m.RUN_CONTEXT['displayGeometry']={'width':1440,'height':3120}
        m.BUSINESS_DEADLINE=m.RESERVATION_DEADLINE=m.SETTLEMENT_DEADLINE=m.BOOKING_OPENS=None
        self.creds=self.root/'creds.json';self.creds.write_text(json.dumps({'username':'TEST','password':'TEST'}))
        patch.object(m,'WEGOLIBRARY_CREDENTIALS',self.creds).start()
    def test_unauthorized_seat_never_reaches_tap(self):
        with patch.object(m,'tap_visual') as tap:
            with self.assertRaises(m.RestartFlow):m.submit_and_settle((target(selected_seat='其它座位'),945,2048),self.root,1440,3120,None)
            tap.assert_not_called();self.assertIsNone(m.pending_reservation())
    def test_primary_backup_identity_mismatch_blocked(self):
        with patch.object(m,'tap_visual') as tap:
            with self.assertRaises(m.RestartFlow):m.submit_and_settle((target(selection='backup'),945,2048),self.root,1440,3120,None)
            tap.assert_not_called()
    def test_popup_never_submits_background_seat(self):
        with patch.object(m,'tap_visual') as tap:
            with self.assertRaises(m.RestartFlow):m.submit_and_settle((target(transient_auth_popup=True),945,2048),self.root,1440,3120,None)
            tap.assert_not_called()
    def test_already_owned_result_never_submits(self):
        with patch.object(m,'tap_visual') as tap:
            with self.assertRaises(m.RestartFlow):m.submit_and_settle((target(success=True),945,2048),self.root,1440,3120,None)
            tap.assert_not_called()
    def test_both_votes_must_agree_on_seat_identity(self):
        result=m.merge_visual_votes(target(),target(selected_seat='其它座位'))
        self.assertFalse(result['target_found']);self.assertTrue(result['seatIdentityMismatch'])
    def test_canonical_authorized_seat_is_accepted(self):
        with patch.object(m,'tap_visual') as tap,patch.object(m,'settle_pending',return_value={}):
            m.submit_and_settle((target(selected_seat='博学楼四楼半圆弧平台 7号'),945,2048),self.root,1440,3120,None)
            tap.assert_called_once()
    def test_missing_frame_never_clicks(self):
        item=target();item.pop('_visualSnapshotId')
        with patch.object(m,'fast_coordinate') as action:
            with self.assertRaises(m.ActionNotSubmitted):m.tap_visual(item,945,2048,1440,3120)
            action.assert_not_called()
    def test_tap_is_bound_to_original_not_latest_snapshot(self):
        m.RUN_CONTEXT['visualFrame']={'snapshotId':'newer-unrelated'}
        with patch.object(m,'fast_coordinate') as action,patch.object(m,'validate_visual_frame',return_value='frame-verified'):
            m.tap_visual(target(),945,2048,1440,3120)
            action.assert_called_once_with('tap-bound','frame-verified',720,1560)
    def test_android_stale_is_returned_without_reobserve(self):
        p=subprocess.CompletedProcess([],1,json.dumps({'ok':False,'result':{'code':'target_stale'}}),'')
        with patch.object(m.subprocess,'run',return_value=p) as request:
            with self.assertRaises(m.ActionNotSubmitted):real_fast_coordinate('tap-bound','frame-old',100,100)
            self.assertEqual(request.call_count,1)
    def test_screenshot_preserves_bound_identity(self):
        result={'ok':True,'snapshotId':'frame-source','frame':{'base64':base64.b64encode(b'image').decode(),'width':945,'height':2048}}
        with patch.object(m,'run_json',return_value=result):
            self.assertEqual(m.screenshot()[0],b'image');self.assertEqual(m.RUN_CONTEXT['visualFrame']['snapshotId'],'frame-source')
    def test_screenshot_without_frame_id_is_rejected(self):
        with patch.object(m,'run_json',return_value={'ok':True}):
            with self.assertRaises(m.FlowError):m.screenshot()
    def test_inspect_keeps_capture_identity_even_if_context_changes(self):
        def shot():m.RUN_CONTEXT['visualFrame']={'snapshotId':'original'};return b'pixels','image/jpeg',945,2048
        def vote(*a):m.RUN_CONTEXT['visualFrame']={'snapshotId':'later'};return target()
        with patch.object(m,'screenshot',side_effect=shot),patch.object(m,'vision',side_effect=vote):
            result,_,_=m.inspect('test',self.root,'id',consensus=True)
            self.assertEqual(result['_visualSnapshotId'],'original')
    def test_cross_day_is_terminal_review_not_retryable_error(self):
        m.save_pending({'date':'2000-01-01','status':'submission_uncertain'})
        with self.assertRaises(m.CrossDaySubmission):m.settle_pending(self.root)
    def test_cross_day_stops_main_after_one_attempt(self):
        m.save_pending({'date':'2000-01-01','status':'submission_uncertain'})
        future=(m.datetime.now(m.TZ)+timedelta(minutes=5)).strftime('%H:%M')
        with patch.object(m.sys,'argv',['audit','--until',future]),patch.object(m,'final_checkin_once',return_value={'success':True}),patch.object(m,'settle_pending',wraps=m.settle_pending) as settle,patch.object(m,'notify_review_image',return_value={}),patch.object(m,'ensure_node'):
            self.assertEqual(m.main(),0);self.assertEqual(settle.call_count,1)
    def test_accepted_post_then_get_timeout_reconciles_without_post(self):
        earlier=(m.datetime.now(m.TZ)-timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')
        status=dict(is_configured=True,wechat_connection_status='connected',last_checkin=earlier,last_checkin_result='签到成功')
        with patch.object(m,'_wegolibrary_request',side_effect=[{'access_token':'TEST'},status,{'success':True},m.WegoLibraryError('GET timeout')]):
            with self.assertRaises(m.WegoLibraryError):m.complete_wegolibrary_checkin(force=True)
        fresh={**status,'last_checkin':m.now_text()}
        with patch.object(m,'_wegolibrary_request',side_effect=[{'access_token':'TEST'},fresh]) as api:
            result=m.complete_wegolibrary_checkin(force=True)
            self.assertTrue(result['reconciledAcceptedRequest']);self.assertEqual(api.call_count,2)
            self.assertNotIn('/api/checkin',[c.args[0] for c in api.call_args_list])
    def test_accepted_but_unchanged_status_does_not_post(self):
        old=(m.datetime.now(m.TZ)-timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')
        m.write_private_json(self.root/'checkin-verification.json',dict(transaction=m.datetime.now(m.TZ).strftime('%Y-%m-%d'),date=m.datetime.now(m.TZ).strftime('%Y-%m-%d'),acceptedAt=m.now_text(),previousTime=old))
        status=dict(is_configured=True,wechat_connection_status='connected',last_checkin=old,last_checkin_result='签到成功')
        with patch.object(m,'_wegolibrary_request',side_effect=[{'access_token':'TEST'},status]) as api:
            with self.assertRaises(m.WegoLibraryError):m.complete_wegolibrary_checkin(force=True)
            self.assertEqual(api.call_count,2)
    def test_receipt_survives_restart(self):
        self.test_accepted_post_then_get_timeout_reconciles_without_post()
        m.RUN_CONTEXT.pop('checkinReceipt')
        status=dict(is_configured=True,wechat_connection_status='connected',last_checkin=m.now_text(),last_checkin_result='签到成功')
        with patch.object(m,'_wegolibrary_request',side_effect=[{'access_token':'TEST'},status]) as api:
            self.assertTrue(m.complete_wegolibrary_checkin(force=True)['success']);self.assertEqual(api.call_count,2)
    def test_final_append_still_sends_one_post(self):
        self.test_accepted_post_then_get_timeout_reconciles_without_post()
        status=dict(is_configured=True,wechat_connection_status='connected',last_checkin=m.now_text(),last_checkin_result='签到成功')
        with patch.object(m,'_wegolibrary_request',side_effect=[{'access_token':'TEST'},status,{'success':True},status]) as api:
            m.complete_wegolibrary_checkin(force=True,require_new_timestamp=False)
            self.assertEqual(sum(c.args[0]=='/api/checkin' for c in api.call_args_list),1)
    def test_corrupt_receipt_never_reposts(self):
        (self.root/'checkin-verification.json').write_text('corrupt')
        status=dict(is_configured=True,wechat_connection_status='connected')
        with patch.object(m,'_wegolibrary_request',side_effect=[{'access_token':'TEST'},status]) as api:
            with self.assertRaises(m.WegoLibraryError):m.complete_wegolibrary_checkin(force=True)
            self.assertEqual(api.call_count,2)
    def test_final_append_bypasses_corrupt_receipt(self):
        (self.root/'checkin-verification.json').write_text('corrupt')
        status=dict(is_configured=True,wechat_connection_status='connected',last_checkin=m.now_text(),last_checkin_result='签到成功')
        with patch.object(m,'_wegolibrary_request',side_effect=[{'access_token':'TEST'},status,{'success':True},status]) as api:
            self.assertTrue(m.complete_wegolibrary_checkin(force=True,require_new_timestamp=False)['success'])
            self.assertEqual(sum(c.args[0]=='/api/checkin' for c in api.call_args_list),1)
    def test_disk_log_failure_does_not_skip_cleanup(self):
        with patch.object(m.sys,'argv',['audit','--max-attempts','1','--finish-phone']),patch.object(m,'model_preflight'),patch.object(m,'ensure_node'),patch.object(m,'run_once',side_effect=m.WindowClosed('failure')),patch.object(m,'final_checkin_once',return_value={'success':False}),patch.object(m,'diagnose_failures',return_value={}),patch.object(m,'write_run',side_effect=OSError),patch.object(m,'finish_with_budget',return_value={}) as cleanup:
            self.assertEqual(m.main(),1);cleanup.assert_called_once()
    def test_model_timeout_retry_shares_one_budget(self):
        with patch.object(m,'provider',return_value=('http://invalid','TEST')),patch.object(m.time,'monotonic',side_effect=[0,0,45.1,45.1]),patch.object(m.urllib.request,'urlopen',side_effect=TimeoutError) as model:
            with self.assertRaises(m.VisionProviderError):m.vision(b'image','image/jpeg','test')
            self.assertEqual(model.call_count,1)
    def image(self,draw=None):
        from PIL import Image,ImageDraw
        im=Image.new('RGB',(945,2048),'white')
        ImageDraw.Draw(im).rectangle((400,900,540,1100),fill='blue')
        if draw:draw(ImageDraw.Draw(im))
        stream=io.BytesIO();im.save(stream,format='PNG');return stream.getvalue()
    def test_identical_pixels_can_rebind_accessibility_epoch(self):
        raw=self.image();path=self.root/'original.jpg';path.write_bytes(raw)
        m.RUN_CONTEXT['visualFrame']={'snapshotId':'frame-new'}
        with patch.object(m,'screenshot',return_value=(raw,'image/png',945,2048)):
            self.assertEqual(m.validate_visual_frame(target(_visualImagePath=str(path)),945,2048),'frame-new')
    def test_changed_target_pixels_rejected(self):
        before=self.image();after=self.image(lambda d:d.rectangle((420,970,530,1050),fill='red'))
        self.assertFalse(m.compare_visual_frames(before,after,500,500)['match'])
    def test_modal_overlay_rejected(self):
        before=self.image();after=self.image(lambda d:d.rectangle((0,800,945,1300),fill='gray'))
        self.assertFalse(m.compare_visual_frames(before,after,500,500)['match'])
    def test_dark_mode_transition_rejected(self):
        before=self.image();after=self.image(lambda d:d.rectangle((0,0,945,2048),fill='black'))
        self.assertFalse(m.compare_visual_frames(before,after,500,500)['match'])
    def test_status_clock_change_ignored_for_body_target(self):
        before=self.image();after=self.image(lambda d:d.rectangle((650,10,850,65),fill='black'))
        self.assertTrue(m.compare_visual_frames(before,after,500,500)['match'])
    def test_status_icons_do_not_pollute_close_button_patch(self):
        before=self.image();after=self.image(lambda d:d.rectangle((30,10,120,65),fill='black'))
        self.assertTrue(m.compare_visual_frames(before,after,58,72)['match'])
    def test_changed_pixels_do_not_reach_device(self):
        before=self.image();path=self.root/'original.jpg';path.write_bytes(before)
        after=self.image(lambda d:d.rectangle((420,970,530,1050),fill='red'))
        with patch.object(m,'screenshot',return_value=(after,'image/png',945,2048)),patch.object(m,'fast_coordinate') as tap:
            with self.assertRaises(m.ActionNotSubmitted):m.tap_visual(target(_visualImagePath=str(path)),945,2048,1440,3120)
            tap.assert_not_called()

if __name__=='__main__':unittest.main()
