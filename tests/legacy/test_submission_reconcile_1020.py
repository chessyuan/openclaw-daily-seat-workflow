import importlib.util,json,tempfile,unittest
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
s=importlib.util.spec_from_file_location('m','/tmp/android-signin-candidate-1020.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)

def state(kind,**kw):
    return dict(reservation_state=kind,success=kind=='owned',confidence=.99,target_found=False,
                selected_seat='博学楼四楼半圆弧平7号',ownership_evidence='您还没有选座哦' if kind=='unowned_home' else '学习中',**kw)

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.folder=self.root/'attempt';self.folder.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(m,'ROOT',self.root).start();patch.object(m.time,'sleep').start()
        patch.object(m,'ensure_node').start()
        patch.object(m,'fast_coordinate',side_effect=AssertionError('Unexpected device action')).start()
        patch.object(m,'invoke',side_effect=AssertionError('Unexpected RPC')).start()
        patch.object(m,'provider',side_effect=AssertionError('Unexpected model request')).start()
        m.BUSINESS_DEADLINE=m.BOOKING_OPENS=m.SETTLEMENT_DEADLINE=None
        m.RESERVATION_DEADLINE=m.datetime.now(m.TZ)+timedelta(minutes=5)
        m.RUN_CONTEXT.clear();m.RUN_CONTEXT['displayGeometry']={'width':1440,'height':3120}
        self.pending={'date':m.datetime.now(m.TZ).strftime('%Y-%m-%d'),'status':'submission_uncertain',
            'submittedAt':(m.datetime.now(m.TZ)-timedelta(seconds=60)).strftime('%Y-%m-%d %H:%M:%S'),
            'seat':'博学楼四楼半圆弧平7号'}
        m.save_pending(self.pending)
    def test_success_api_does_not_wait_for_vision(self):
        with patch.object(m,'complete_wegolibrary_checkin',return_value={'success':True}),patch.object(m,'inspect_submission_state') as see:
            m.settle_pending(self.folder);see.assert_not_called()
            self.assertEqual(m.pending_reservation()['status'],'settled')
    def test_no_seat_rejection_observes_before_next_api(self):
        with patch.object(m,'complete_wegolibrary_checkin',side_effect=m.WegoLibraryError('你没有选座或没有专座')) as api,patch.object(m,'reconcile_submission',side_effect=m.WegoLibraryError('unknown',30)) as see:
            for _ in range(2):
                with self.assertRaises(m.WegoLibraryError):m.settle_pending(self.folder)
            self.assertEqual(api.call_count,1);self.assertEqual(see.call_count,2)
            self.assertTrue(m.pending_reservation()['noSeatRejected'])
    def test_owned_keeps_pending_without_rebooking(self):
        with patch.object(m,'inspect_submission_state',return_value=state('owned')),patch.object(m,'refresh_seat_page') as refresh:
            self.assertEqual(m.reconcile_submission(self.folder,self.pending),'owned')
            self.assertEqual(m.pending_reservation()['status'],'owned_unconfirmed');refresh.assert_not_called()
    def test_popup_is_confirmed_then_observed_again(self):
        with patch.object(m,'inspect_submission_state',side_effect=[state('login_popup'),state('owned')]),patch.object(m,'dismiss_school_binding_popup') as dismiss:
            self.assertEqual(m.reconcile_submission(self.folder,self.pending),'owned');dismiss.assert_called_once()
    def test_loading_is_waited_not_blind_clicked(self):
        with patch.object(m,'inspect_submission_state',side_effect=[state('loading'),state('owned')]):
            self.assertEqual(m.reconcile_submission(self.folder,self.pending),'owned')
    def test_advertisement_back_once_then_reobserve(self):
        with patch.object(m,'inspect_submission_state',side_effect=[state('advertisement'),state('owned')]),patch.object(m,'fast_coordinate') as action:
            m.reconcile_submission(self.folder,self.pending);action.assert_called_once_with('global','back')
    def test_fresh_unowned_before_deadline_archives_and_restarts(self):
        with patch.object(m,'inspect_submission_state',return_value=state('unowned_home')),patch.object(m,'refresh_seat_page') as refresh:
            with self.assertRaises(m.RestartFlow):m.reconcile_submission(self.folder,self.pending)
            self.assertIsNone(m.pending_reservation());refresh.assert_called_once()
            self.assertTrue((self.folder/'submission-proven-not-completed.json').is_file())
    def test_unowned_after_cutoff_cannot_rebook(self):
        m.RESERVATION_DEADLINE=m.datetime.now(m.TZ)-timedelta(seconds=1)
        with patch.object(m,'inspect_submission_state',return_value=state('unowned_home')),patch.object(m,'refresh_seat_page'):
            with self.assertRaises(m.NoReservationAfterCutoff):m.reconcile_submission(self.folder,self.pending)
            self.assertIsNotNone(m.pending_reservation())
    def test_manual_run_without_booking_window_cannot_clear_pending(self):
        m.RESERVATION_DEADLINE=None
        with patch.object(m,'inspect_submission_state',return_value=state('unowned_home')),patch.object(m,'refresh_seat_page'):
            with self.assertRaises(m.NoReservationAfterCutoff):m.reconcile_submission(self.folder,self.pending)
            self.assertIsNotNone(m.pending_reservation())
    def test_fresh_owned_proof_never_clears_pending(self):
        with patch.object(m,'inspect_submission_state',side_effect=[state('unowned_home'),state('owned')]),patch.object(m,'refresh_seat_page'):
            self.assertEqual(m.reconcile_submission(self.folder,self.pending),'owned')
            self.assertIsNotNone(m.pending_reservation())
    def test_refresh_timeout_keeps_submission(self):
        with patch.object(m,'inspect_submission_state',return_value=state('unowned_home')),patch.object(m,'refresh_seat_page',side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):m.reconcile_submission(self.folder,self.pending)
            self.assertIsNotNone(m.pending_reservation())
    def test_unknown_is_bounded_two_rounds(self):
        with patch.object(m,'inspect_submission_state',return_value=state('unknown')):
            with self.assertRaises(m.WegoLibraryError):m.reconcile_submission(self.folder,self.pending)
            with self.assertRaises(m.SubmissionNeedsReview):m.reconcile_submission(self.folder,m.pending_reservation())
            self.assertEqual(m.pending_reservation()['unknownReviews'],2)
    def test_unknown_two_rounds_make_only_one_empty_api_call(self):
        with patch.object(m,'inspect_submission_state',return_value=state('unknown')),patch.object(m,'complete_wegolibrary_checkin',side_effect=m.WegoLibraryError('你没有选座或没有专座',60)) as api:
            with self.assertRaises(m.WegoLibraryError):m.settle_pending(self.folder)
            with self.assertRaises(m.SubmissionNeedsReview):m.settle_pending(self.folder)
            self.assertEqual(api.call_count,1)
    def test_recent_click_is_not_discarded_as_delayed_submission(self):
        self.pending['submittedAt']=m.now_text();m.save_pending(self.pending)
        with patch.object(m,'inspect_submission_state',return_value=state('unowned_home')),patch.object(m,'refresh_seat_page'):
            with self.assertRaises(m.WegoLibraryError):m.reconcile_submission(self.folder,self.pending)
            self.assertIsNotNone(m.pending_reservation())
    def test_loading_loop_bounded_three_observations(self):
        with patch.object(m,'inspect_submission_state',return_value=state('loading')) as see:
            with self.assertRaises(m.WegoLibraryError):m.reconcile_submission(self.folder,self.pending)
            self.assertEqual(see.call_count,3)
    def test_model_disagreement_is_unknown(self):
        with patch.object(m,'screenshot',return_value=(b'img','image/jpeg',945,2048)),patch.object(m,'vision',side_effect=[state('owned'),state('unowned_home')]):
            self.assertEqual(m.inspect_submission_state(self.folder,'votes')['reservation_state'],'unknown')
    def test_low_confidence_cannot_prove_unowned(self):
        a=state('unowned_home');a['confidence']=.5
        with patch.object(m,'screenshot',return_value=(b'img','image/jpeg',945,2048)),patch.object(m,'vision',return_value=a):
            self.assertEqual(m.inspect_submission_state(self.folder,'votes')['reservation_state'],'unknown')
    def test_owned_seat_disagreement_is_unknown(self):
        b=state('owned');b['selected_seat']='博学楼三楼半圆弧平7号'
        with patch.object(m,'screenshot',return_value=(b'img','image/jpeg',945,2048)),patch.object(m,'vision',side_effect=[state('owned'),b]):
            self.assertEqual(m.inspect_submission_state(self.folder,'votes')['reservation_state'],'unknown')
    def test_agreed_state_only_review_not_reported_as_coordinate_disagreement(self):
        with patch.object(m,'screenshot',return_value=(b'img','image/jpeg',945,2048)),patch.object(m,'vision',return_value=state('owned')):
            result=m.inspect_submission_state(self.folder,'votes')
            self.assertEqual(result['reservation_state'],'owned')
            self.assertIn('双重只读状态复核一致',result['evidence'])
            self.assertNotIn('未达成一致',result['evidence'])
    def test_unowned_claim_needs_visible_text(self):
        a=state('unowned_home');a['ownership_evidence']='unsure'
        with patch.object(m,'inspect',return_value=(a,945,2048)):
            self.assertEqual(m.inspect_submission_state(self.folder,'votes')['reservation_state'],'unknown')
    def test_owned_wrong_seat_is_not_authorized(self):
        a=state('owned');a['selected_seat']='其他座位'
        with patch.object(m,'inspect',return_value=(a,945,2048)):
            self.assertEqual(m.inspect_submission_state(self.folder,'votes')['reservation_state'],'unknown')
    def test_finish_deadline_restored_on_error(self):
        previous=m.datetime.now(m.TZ)+timedelta(hours=1);m.BUSINESS_DEADLINE=previous
        with patch.object(m,'best_effort_finish',side_effect=TimeoutError):
            self.assertIn('cleanupError',m.finish_with_budget(self.folder))
            self.assertEqual(m.BUSINESS_DEADLINE,previous)
    def test_expired_cron_retry_has_no_business_actions(self):
        past=(m.datetime.now(m.TZ)-timedelta(minutes=5)).strftime('%H:%M')
        with patch.object(m.sys,'argv',['signin','--until',past]),patch.object(m,'run_once') as run,patch.object(m,'final_checkin_once') as final:
            self.assertEqual(m.main(),0);run.assert_not_called();final.assert_not_called()
    def main_failure(self,final_success=False):
        (self.root/'pending-reservation.json').unlink()
        events=[]
        with patch.object(m.sys,'argv',['signin','--max-attempts','1','--finish-phone']),patch.object(m,'run_once',side_effect=m.WindowClosed('test')),patch.object(m,'model_preflight'),patch.object(m,'diagnose_failures',return_value={}),patch.object(m,'final_checkin_once',side_effect=lambda *a:events.append('final') or {'success':final_success}) as final,patch.object(m,'best_effort_finish',side_effect=lambda *a,**kw:events.append('finish') or {}):
            result=m.main();self.assertEqual(final.call_count,1)
        self.assertEqual(events,['final','finish']);return result
    def test_final_api_precedes_failure_cleanup(self):self.assertEqual(self.main_failure(),1)
    def test_final_api_rescue_is_reported_success(self):self.assertEqual(self.main_failure(True),0)
    def test_outside_review_image_never_sent(self):
        with patch.object(m.subprocess,'run') as send:
            m.notify_review_image('/etc/passwd',None);send.assert_not_called()
    def test_resume_uses_fresh_target_and_complete_transaction(self):
        target=(dict(target_found=True,success=False,selected_seat='博学楼四楼半圆弧平7号',selection='primary'),945,2048)
        m.RUN_CONTEXT['reservationRestartReady']=True
        with patch.object(m,'wait_target',return_value=target),patch.object(m,'submit_and_settle',return_value={'success':True}) as submit:
            m.resume_verified_reservation(self.folder,m.RESERVATION_DEADLINE)
            submit.assert_called_once();self.assertNotIn('reservationRestartReady',m.RUN_CONTEXT)
    def test_resume_owned_does_not_rebook(self):
        target=(dict(target_found=False,success=True,selected_seat='博学楼四楼半圆弧平7号'),945,2048)
        with patch.object(m,'wait_target',return_value=target),patch.object(m,'settle_pending',return_value={}) as settle,patch.object(m,'submit_and_settle') as submit:
            m.resume_verified_reservation(self.folder,m.RESERVATION_DEADLINE)
            settle.assert_called_once();submit.assert_not_called()
    def test_resume_unauthorized_seat_rejected(self):
        target=(dict(target_found=True,success=False,selected_seat='其他座位',selection='primary'),945,2048)
        with patch.object(m,'wait_target',return_value=target),patch.object(m,'submit_and_settle') as submit:
            with self.assertRaises(m.RestartFlow):m.resume_verified_reservation(self.folder,m.RESERVATION_DEADLINE)
            submit.assert_not_called()
    def test_resume_expired_never_calls_target_or_submit(self):
        with patch.object(m,'wait_target') as see,patch.object(m,'submit_and_settle') as submit:
            with self.assertRaises(m.WindowClosed):m.resume_verified_reservation(self.folder,m.datetime.now(m.TZ)-timedelta(seconds=1))
            see.assert_not_called();submit.assert_not_called()
    def test_verified_resubmission_count_bounded(self):
        m.RUN_CONTEXT['verifiedResubmissionCount']=2
        with patch.object(m,'inspect_submission_state',return_value=state('unowned_home')),patch.object(m,'refresh_seat_page'):
            with self.assertRaises(m.SubmissionNeedsReview):m.reconcile_submission(self.folder,self.pending)
            self.assertIsNotNone(m.pending_reservation())
    def test_prior_owned_evidence_cannot_be_discarded(self):
        self.pending['status']='owned_unconfirmed';m.save_pending(self.pending)
        with patch.object(m,'inspect_submission_state',return_value=state('unowned_home')),patch.object(m,'refresh_seat_page'):
            with self.assertRaises(m.SubmissionNeedsReview):m.reconcile_submission(self.folder,self.pending)
            self.assertIsNotNone(m.pending_reservation())

if __name__=='__main__':unittest.main()
