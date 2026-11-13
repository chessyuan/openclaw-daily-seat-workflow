"""Stateful full control-flow simulation, not a claim of live seat booking.

The real run_once/main, pending journal, settlement, receipt and deadlines run.
Only Android observations/gestures, model service and HTTP transport are doubles.
All state, credentials (TEST only) and artifacts live in a TemporaryDirectory.
"""
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

SOURCE = os.environ.get('SIGNIN_SOURCE', '/tmp/android-signin-1103-lean.py')
spec = importlib.util.spec_from_file_location('signin', SOURCE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
ROWS = []


class Lab:
    def __init__(self, root, **conditions):
        self.root, self.conditions = root, conditions
        self.now = datetime(2026, 11, 4, 6, 28, 20, tzinfo=m.TZ)
        if conditions.get('late_start'):
            self.now = self.now.replace(minute=31)
        self.start = self.now
        self.page = conditions.get('start_page', 'list')
        self.package = m.WECHAT
        self.reserved = conditions.get('owned', False)
        self.checked = False
        self.events = []
        self.page_opens = self.bookings = self.posts = self.observations = 0
        self.popup_left = conditions.get('popup', 0)
        self.loading_left = conditions.get('loading', 0)
        self.offline_left = conditions.get('offline', 0)
        self.provider_left = conditions.get('provider_failure', 0)
        self.get_failures = conditions.get('status_get_failure', 0)
        self.after_post_failures = conditions.get('accepted_get_failure', 0)
        self.checkin_failures = conditions.get('checkin_delay', 0)
        self.stale_left = conditions.get('navigation_stale', 0)
        self.menu_stale_left = conditions.get('menu_stale', 0)
        self.last_checkin = ''
        self.last_result = ''
        self.booked_at = None
        lab = self
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return lab.now if tz is not None else lab.now.replace(tzinfo=None)
        self.clock = Clock

    def tick(self, seconds):
        self.now += timedelta(seconds=max(.01, seconds))

    def event(self, event, **fields):
        self.events.append(dict(event=event, time=self.now.strftime('%H:%M:%S'), **fields))

    def ensure_node(self):
        if self.offline_left:
            self.offline_left -= 1
            self.tick(1)
            raise m.FlowError('Android Node 离线')
        m.RUN_CONTEXT['nodeConnection'] = {'connected': True}

    def preflight(self):
        if self.provider_left:
            self.provider_left -= 1
            raise m.VisionProviderError('TimeoutError')

    def prepare(self):
        if self.conditions.get('locked'):
            raise m.FlowError('手机需要人工解锁。')
        m.RUN_CONTEXT['displayGeometry'] = {'width':1440, 'height':3120}
        self.tick(2)
        return 1440, 3120

    def inspect(self, instruction, folder, label, **kwargs):
        self.tick(self.conditions.get('observation_seconds', 2))
        self.observations += 1
        result = dict(page=self.page, target_found=False, confidence=.99,
            secure_lock=False, loading=False, transient_auth_popup=False,
            school_binding_popup=False, dialog_text='', confirm_text='',
            success=False, x_norm=300, y_norm=600, evidence='controlled UI observation',
            _visualSnapshotId='lab-'+str(self.observations))
        if 'conversation-' in label and 'popup' not in label:
            result.update(entry_route={'list':'conversation_list','chat':'official_conversation',
                'web':'library_webview'}.get(self.page, 'other'),
                target_found=self.page=='list', lab_action='conversation')
        elif 'route-' in label:
            result.update(page='library_webview' if self.page=='web' else 'official_conversation',
                target_found=True, lab_action='menu')
        elif 'popup' in label:
            if self.popup_left:
                text = self.conditions.get('popup_text', '尚未登录')
                result.update(page='login_popup', target_found=True, transient_auth_popup=True,
                    dialog_text=text, confirm_text='确定', lab_action='confirm')
            elif self.loading_left:
                self.loading_left -= 1
                result.update(page='loading', loading=True)
            else:
                result.update(page='home', success=True)
        elif label.startswith('favorite') or 'stale-seat' in label or 'live-booking-target' in label:
            if self.conditions.get('missing'):
                result.update(selection='none')
            elif self.reserved:
                result.update(success=True, selection='owned', selected_seat='博学楼四楼半圆弧平7号')
            else:
                backup = self.conditions.get('backup', False)
                result.update(target_found=True, lab_action='book',
                    selection='backup' if backup else 'primary',
                    selected_seat='博学楼三楼半圆弧平7号' if backup else '博学楼四楼半圆弧平7号',
                    ownership_evidence='您还没有选座哦')
                if self.conditions.get('wrong_seat'):
                    result['selected_seat'] = '博学楼四楼半圆弧平8号'
        elif label.startswith('live-seat-'):
            result.update(action='unavailable', verified_live=True, target_found=False)
        elif instruction == m.SUBMISSION_STATE_PROMPT:
            result.update(reservation_state='owned' if self.reserved else 'unowned_home',
                success=self.reserved, selected_seat='博学楼四楼半圆弧平7号',
                ownership_evidence='本人学习中' if self.reserved else '您还没有选座哦')
        else:
            result.update(target_found=True, page='official_conversation', lab_action='menu')
        return result, 945, 2048

    def tap(self, result, *args, **kwargs):
        action = result.get('lab_action')
        if self.menu_stale_left and action == 'menu' and self.now >= m.BOOKING_OPENS:
            self.menu_stale_left -= 1
            raise m.ActionNotSubmitted('Menu changed during opening wait; no tap')
        if self.stale_left and action in ('conversation','menu'):
            self.stale_left -= 1
            raise m.ActionNotSubmitted('native snapshot changed; gesture not submitted')
        self.event('tap', action=action)
        if action == 'conversation':
            self.page = 'chat'
        elif action == 'menu':
            self.page = 'web'
            self.page_opens += 1
            self.event('open_web')
        elif action == 'confirm':
            self.popup_left -= 1
            self.event('confirm_popup')
        elif action == 'book':
            journal = m.pending_reservation()
            assert journal and journal['status'] == 'submission_uncertain', 'Booking before durable journal'
            assert not self.reserved, 'Duplicate reservation mutation'
            assert self.now >= m.BOOKING_OPENS, 'Booking before opening'
            self.reserved = True
            self.bookings += 1
            self.booked_at = self.now
            self.event('reserve', seat=result['selected_seat'])
            if self.conditions.get('cross_cutoff'):
                self.now = self.now.replace(minute=43, second=2)
            if self.conditions.get('booking_timeout'):
                raise TimeoutError('Booking RPC outcome unknown after accepted gesture')
        self.tick(.2)
        return {'ok':True}

    def invoke(self, command, params, **kwargs):
        if command == 'mobile.ui.observe':
            return {'package':self.package, 'snapshotId':'lab-current', 'nodes':[]}
        if command == 'mobile.ui.act':
            name = params['action'].get('name')
            self.event('global', name=name)
            if name == 'back':
                self.page = 'chat'
            return {'code':'completed'}
        if command == 'mobile.ui.launch':
            return {'code':'completed'}
        raise AssertionError('Unexpected RPC '+command)

    def coordinate(self, *args):
        assert args[0] == 'swipe', 'Unexpected unmodeled coordinate action'
        assert self.page == 'web', 'Scroll outside intended page'
        self.event('scroll')
        return {'ok':True}

    def api(self, path, token=None, data=None, form=False, timeout=25):
        self.tick(self.conditions.get('api_seconds', .3))
        if path == '/api/auth/login':
            return {'access_token':'TEST'}
        if path == '/api/status':
            if self.get_failures or (self.checked and self.after_post_failures):
                if self.get_failures:
                    self.get_failures -= 1
                else:
                    self.after_post_failures -= 1
                raise m.WegoLibraryError('synthetic readonly disconnect', transient_read=True)
            return dict(is_configured=True, wechat_connection_status='connected',
                        last_checkin=self.last_checkin, last_checkin_result=self.last_result)
        assert path == '/api/checkin', 'Unexpected HTTP endpoint'
        self.posts += 1
        self.event('checkin_post')
        if not self.reserved:
            return {'success':False, 'message':'你没有选座或没有专座'}
        if self.checkin_failures:
            self.checkin_failures -= 1
            return {'success':False, 'message':'你没有选座或没有专座'}
        self.checked = True
        self.last_checkin = self.now.strftime('%Y-%m-%d %H:%M:%S')
        self.last_result = '签到成功：到馆验证成功'
        return {'success':True, 'message':self.last_result}

    def finish(self, *args, **kwargs):
        self.event('cleanup')
        if self.conditions.get('cleanup_failure'):
            raise OSError('synthetic cosmetic cleanup failure')
        return {'cleanup':{'cleared':True}, 'screenOff':{'requested':True}}

    def notify(self, *args, **kwargs):
        self.event('notify')
        return {'sent':True, 'accepted_by':'test-double'}

    def patches(self):
        patches = {'ROOT':self.root, 'datetime':self.clock,
            'WEGOLIBRARY_CREDENTIALS':self.root/'credentials.json',
            'ensure_node':self.ensure_node, 'model_preflight':self.preflight,
            'prepare_display':self.prepare, 'inspect':self.inspect, 'tap_visual':self.tap,
            'invoke':self.invoke, 'fast_coordinate':self.coordinate,
            '_wegolibrary_request_once':self.api, 'best_effort_finish':self.finish,
            'notify_wechat':self.notify,
            'diagnose_failures':lambda *a,**k: {'stage':'test', 'diagnosis':'controlled failure'},
            'recover_page':lambda *a,**k: {'action':'wait'}}
        stack = contextlib.ExitStack()
        for name, value in patches.items():
            stack.enter_context(patch.object(m, name, value))
        stack.enter_context(patch.object(m.time, 'sleep', self.tick))
        stack.enter_context(patch.object(m.sys, 'argv', ['android-signin', '--not-before','06:30',
            '--until','06:43','--settle-until','06:50','--finish-phone']))
        (self.root/'credentials.json').write_text(json.dumps({'username':'TEST','password':'TEST'}))
        return stack


class WorkflowMatrix(unittest.TestCase):
    def run_case(self, name, success=True, **conditions):
        with tempfile.TemporaryDirectory() as directory:
            lab = Lab(Path(directory), **conditions)
            m.RUN_CONTEXT.clear()
            with lab.patches(), contextlib.redirect_stdout(io.StringIO()):
                code = m.main()
                pending = m.pending_reservation()
            row = dict(case=name, kind='isolated-stateful-control-flow-simulation',
                exitCode=code, expectedSuccess=success, booked=lab.bookings,
                checkinPosts=lab.posts, pageOpens=lab.page_opens,
                observations=lab.observations, endedAt=lab.now.isoformat(), events=lab.events,
                pendingStatus=(pending or {}).get('status'))
            ROWS.append(row)
            self.assertEqual(code, 0 if success else 1, row)
            self.assertLessEqual(lab.bookings, 1, row)
            self.assertLess(lab.observations, 600, 'Unbounded page loop')
            if success:
                self.assertTrue(lab.checked)
                self.assertEqual(pending['status'], 'settled')
                self.assertEqual(lab.events[-1]['event'], 'cleanup')
                reserve = next((i for i,x in enumerate(lab.events) if x['event']=='reserve'), None)
                if reserve is not None:
                    next_events = lab.events[reserve+1:]
                    self.assertTrue(any(x['event']=='checkin_post' for x in next_events))
                    self.assertFalse(any(x['event']=='open_web' for x in next_events))
            return lab, row

    def test_cold_entry_opens_once_after_opening(self):
        lab, _ = self.run_case('cold-entry')
        opens = [x for x in lab.events if x['event']=='open_web']
        self.assertEqual(len(opens), 1)
        self.assertGreaterEqual(opens[0]['time'], '06:30:00')

    def test_warm_webview_reopens_once_after_opening(self):
        lab, _ = self.run_case('warm-webview', start_page='web')
        self.assertEqual(lab.page_opens, 1)

    def test_warm_official_chat_opens_once_after_opening(self):
        lab, _ = self.run_case('warm-official-chat', start_page='chat')
        self.assertEqual(lab.page_opens, 1)

    def test_menu_changed_during_wait_is_reobserved(self):
        lab, _ = self.run_case('menu-changed-while-waiting', menu_stale=1)
        self.assertEqual(lab.page_opens, 1)
        self.assertEqual(lab.menu_stale_left, 0)

    def test_late_start_does_not_wait_for_next_day(self):
        lab, _ = self.run_case('late-manual-entry', late_start=True)
        self.assertEqual(lab.page_opens, 1)
        self.assertLess(lab.now.minute, 33)

    def test_known_transient_login_popup(self):
        lab, _ = self.run_case('login-confirm', popup=1)
        self.assertEqual(sum(x['event']=='confirm_popup' for x in lab.events), 1)

    def test_school_binding_and_slow_refresh(self):
        self.run_case('binding-slow-refresh', popup=1, popup_text='您的学校需要您绑定账户', loading=2)

    def test_navigation_explicit_rejection(self):
        self.run_case('native-navigation-stale', navigation_stale=1)

    def test_approved_backup(self):
        lab, _ = self.run_case('primary-unavailable-approved-backup', backup=True)
        self.assertEqual(next(x['seat'] for x in lab.events if x['event']=='reserve'), '博学楼三楼半圆弧平7号')

    def test_owned_seat_is_not_reserved_again(self):
        lab, _ = self.run_case('already-owned', owned=True)
        self.assertEqual(lab.bookings, 0)

    def test_unknown_booking_rpc_goes_directly_to_settlement(self):
        lab, _ = self.run_case('booking-rpc-timeout', booking_timeout=True)
        self.assertEqual(lab.bookings, 1)

    def test_readonly_status_retry(self):
        self.run_case('status-get-disconnect', status_get_failure=1)

    def test_post_accepted_status_read_failure_no_extra_post(self):
        lab, _ = self.run_case('accepted-post-get-failure', accepted_get_failure=2)
        self.assertEqual(lab.posts, 2, 'Only confirmed first POST plus user-required final POST')

    def test_backend_propagation_delay(self):
        self.run_case('reservation-propagation-delay', checkin_delay=1)

    def test_node_recovers_without_duplicate_booking(self):
        self.run_case('node-offline-then-online', offline=2)

    def test_model_recovers_without_blind_click(self):
        self.run_case('vision-unavailable-then-recovers', provider_failure=1)

    def test_booking_cutoff_does_not_abandon_pending_settlement(self):
        lab, _ = self.run_case('accepted-booking-crosses-cutoff', cross_cutoff=True)
        self.assertGreaterEqual(lab.now.minute, 43)

    def test_missing_target_is_bounded_no_booking(self):
        lab, _ = self.run_case('no-target', success=False, missing=True)
        self.assertEqual(lab.bookings, 0)

    def test_unapproved_seat_never_submitted(self):
        lab, _ = self.run_case('unapproved-seat', success=False, wrong_seat=True)
        self.assertEqual(lab.bookings, 0)

    def test_secure_lock_stops_new_booking(self):
        lab, _ = self.run_case('secure-keyguard', success=False, locked=True)
        self.assertEqual(lab.bookings, 0)

    def test_latency_and_fault_combinations(self):
        cases = [
            ('slow-vision-cold', dict(observation_seconds=12, api_seconds=.5)),
            ('slow-vision-popup', dict(observation_seconds=12, popup=1, loading=1)),
            ('temporary-node-and-model', dict(observation_seconds=8, offline=2, provider_failure=1)),
            ('warm-slow-popup', dict(start_page='web', observation_seconds=12, popup=2, loading=2)),
            ('backup-status-delay', dict(backup=True, observation_seconds=8, status_get_failure=1, checkin_delay=1)),
            ('uncertain-rpc-and-reads', dict(booking_timeout=True, observation_seconds=10, accepted_get_failure=2)),
        ]
        for name, conditions in cases:
            with self.subTest(name=name):
                self.run_case(name, **conditions)

    def test_cleanup_failure_cannot_restart_successful_booking(self):
        lab, _ = self.run_case('cleanup-failure', cleanup_failure=True)
        self.assertEqual(lab.bookings, 1)


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        Path('/tmp/workflow-matrix-1103.json').write_text(json.dumps(ROWS, ensure_ascii=False, indent=2))
