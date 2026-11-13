import importlib.util, json, tempfile, unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('m', '/tmp/android-signin-candidate-1021.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

def state(kind):
    return {'reservation_state': kind, 'success': kind == 'owned', 'confidence': .99,
            'selected_seat': '博学楼四楼半圆弧平7号',
            'ownership_evidence': '您还没有选座哦' if kind == 'unowned_home' else '学习中'}

def route(kind):
    return ({'page': kind, 'refresh_route': kind, 'target_found': kind in {'official_conversation', 'library_webview'},
             'confidence': .99, 'x_norm': 500, 'y_norm': 500}, 945, 2048)

class Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.folder = self.root/'attempt'; self.folder.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(m, 'ROOT', self.root).start()
        patch.object(m.time, 'sleep').start()
        patch.object(m, 'ensure_node').start()
        for name in ('provider', 'fast_coordinate'):
            patch.object(m, name, side_effect=AssertionError('Unexpected external action: '+name)).start()
        patch.object(m, 'observe', return_value={'package': m.WECHAT, 'snapshotId': 'native-fresh'}).start()
        self.back = patch.object(m, 'act', return_value={'code': 'completed'}).start()
        m.BUSINESS_DEADLINE = m.SETTLEMENT_DEADLINE = m.BOOKING_OPENS = None
        m.RESERVATION_DEADLINE = m.datetime.now(m.TZ)+timedelta(minutes=5)
        m.RUN_CONTEXT.clear(); m.RUN_CONTEXT['displayGeometry']={'width':1440,'height':3120}
        self.pending={'date':m.datetime.now(m.TZ).strftime('%Y-%m-%d'), 'status':'submission_uncertain',
                      'noSeatRejected':True, 'seat':'博学楼四楼半圆弧平7号',
                      'submittedAt':(m.datetime.now(m.TZ)-timedelta(seconds=60)).strftime('%Y-%m-%d %H:%M:%S')}
        m.save_pending(self.pending)

    def test_exact_morning_popup_refresh_home_retries_without_back(self):
        with patch.object(m,'inspect_submission_state',side_effect=[state('login_popup'),state('unowned_home'),state('unowned_home')]), patch.object(m,'dismiss_school_binding_popup',return_value={'handled':True}), patch.object(m,'refresh_seat_page') as refresh:
            with self.assertRaises(m.RestartFlow): m.reconcile_submission(self.folder,self.pending)
            refresh.assert_not_called()
            self.assertIsNone(m.pending_reservation())
            proof=json.loads((self.folder/'submission-proven-not-completed.json').read_text())
            self.assertEqual(proof['refreshMethod'],'confirmed_modal_auto_refresh')
            self.assertTrue(m.RUN_CONTEXT['reservationRestartReady'])

    def test_popup_refresh_then_owned_keeps_reservation(self):
        with patch.object(m,'inspect_submission_state',side_effect=[state('login_popup'),state('owned')]), patch.object(m,'dismiss_school_binding_popup',return_value={'handled':True}), patch.object(m,'refresh_seat_page') as refresh:
            self.assertEqual(m.reconcile_submission(self.folder,self.pending),'owned')
            refresh.assert_not_called(); self.assertEqual(m.pending_reservation()['status'],'owned_unconfirmed')

    def test_popup_handler_failure_cannot_clear_journal(self):
        with patch.object(m,'inspect_submission_state',return_value=state('login_popup')), patch.object(m,'dismiss_school_binding_popup',side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):m.reconcile_submission(self.folder,self.pending)
            self.assertEqual(m.pending_reservation(),self.pending)

    def test_unconfirmed_auto_refresh_still_requires_reopen(self):
        with patch.object(m,'inspect_submission_state',side_effect=[state('login_popup'),state('unowned_home'),state('unowned_home')]), patch.object(m,'dismiss_school_binding_popup',return_value={'handled':False}), patch.object(m,'refresh_seat_page') as refresh:
            with self.assertRaises(m.RestartFlow):m.reconcile_submission(self.folder,self.pending)
            refresh.assert_called_once()

    def test_no_booking_after_cutoff_stops_instead_of_sixty_second_loop(self):
        m.RESERVATION_DEADLINE=m.datetime.now(m.TZ)-timedelta(seconds=1)
        with patch.object(m,'inspect_submission_state',return_value=state('unowned_home')),patch.object(m,'refresh_seat_page'):
            with self.assertRaises(m.NoReservationAfterCutoff):m.reconcile_submission(self.folder,self.pending)
            self.assertEqual(m.pending_reservation(),self.pending)

    def test_reopen_observes_webview_close_then_conversation_menu(self):
        with patch.object(m,'inspect',side_effect=[route('library_webview'),route('official_conversation')]) as see, patch.object(m,'tap_visual') as tap, patch.object(m,'dismiss_school_binding_popup'),patch.object(m,'scroll_seat_page') as scroll:
            m.refresh_seat_page(self.folder,1440,3120)
            self.assertEqual(see.call_count,2);tap.assert_called_once();scroll.assert_called_once()
            self.back.assert_called_once_with({'package':m.WECHAT,'snapshotId':'native-fresh'}, {'type':'global_action','name':'back'})
            self.assertEqual(tap.call_args.args[0]['refresh_route'],'official_conversation')

    def test_already_in_conversation_does_not_back(self):
        with patch.object(m,'inspect',return_value=route('official_conversation')),patch.object(m,'tap_visual') as tap,patch.object(m,'dismiss_school_binding_popup'),patch.object(m,'scroll_seat_page'):
            m.refresh_seat_page(self.folder,1440,3120);tap.assert_called_once()

    def test_unknown_route_never_clicks(self):
        with patch.object(m,'inspect',return_value=route('unknown')),patch.object(m,'tap_visual') as tap:
            with self.assertRaises(m.RestartFlow):m.refresh_seat_page(self.folder,1440,3120)
            tap.assert_not_called()

    def test_loading_waits_then_observes_without_click(self):
        with patch.object(m,'inspect',side_effect=[route('loading'),route('official_conversation')]),patch.object(m,'tap_visual') as tap,patch.object(m,'dismiss_school_binding_popup'),patch.object(m,'scroll_seat_page'):
            m.refresh_seat_page(self.folder,1440,3120);tap.assert_called_once()

    def test_loading_is_bounded(self):
        with patch.object(m,'inspect',return_value=route('loading')) as see,patch.object(m,'tap_visual') as tap:
            with self.assertRaises(m.RestartFlow):m.refresh_seat_page(self.folder,1440,3120)
            self.assertEqual(see.call_count,6);tap.assert_not_called()

    def test_stale_frame_reclassifies_not_reuses_previous_route(self):
        with patch.object(m,'inspect',side_effect=[route('library_webview'),route('official_conversation')]),patch.object(m,'act',side_effect=m.ActionNotSubmitted('stale')),patch.object(m,'tap_visual') as tap,patch.object(m,'dismiss_school_binding_popup'),patch.object(m,'scroll_seat_page') as scroll:
            m.refresh_seat_page(self.folder,1440,3120)
            tap.assert_called_once();scroll.assert_called_once()

    def test_cutoff_exception_is_not_converted_into_settlement_retry(self):
        future=(m.datetime.now(m.TZ)+timedelta(minutes=5)).strftime('%H:%M')
        settle=(m.datetime.now(m.TZ)+timedelta(minutes=10)).strftime('%H:%M')
        with patch.object(m.sys,'argv',['signin','--until',future,'--settle-until',settle]),patch.object(m,'settle_pending',side_effect=m.NoReservationAfterCutoff('proof')) as run,patch.object(m,'final_checkin_once',return_value={'success':True}) as final:
            self.assertEqual(m.main(),0);run.assert_called_once();final.assert_called_once()

if __name__=='__main__':unittest.main()
