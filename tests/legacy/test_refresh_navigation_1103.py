"""No booking actions: reproduce the rotating-banner navigation dead end."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('signin', os.environ.get(
    'SIGNIN_SOURCE', '/tmp/android-signin-1103-navigation.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def route(page, **changes):
    data = dict(page=page, target_found=True, confidence=.99, x_norm=58,
                y_norm=72, transient_auth_popup=False, school_binding_popup=False,
                secure_lock=False, loading=False, success=False)
    data.update(changes)
    return data, 945, 2048


class RefreshTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.addCleanup(patch.stopall)
        m.RUN_CONTEXT.clear()
        m.BUSINESS_DEADLINE = None
        patch.object(m.time, 'sleep').start()
        self.actions = []

        def invoke(command, params, **kwargs):
            if command == 'mobile.ui.observe':
                return {'package': m.WECHAT, 'snapshotId': 'fresh-native'}
            if command == 'mobile.ui.act':
                self.actions.append(params)
                return {'code': 'completed'}
            raise AssertionError('Unexpected external operation: '+command)
        self.rpc = patch.object(m, 'invoke', side_effect=invoke).start()
        patch.object(m, 'provider', side_effect=AssertionError('No model request in unit test')).start()
        patch.object(m, 'fast_coordinate', side_effect=AssertionError('No coordinates in native Back')).start()
        self.dismiss = patch.object(m, 'dismiss_school_binding_popup', return_value={'handled': False}).start()
        self.scroll = patch.object(m, 'scroll_seat_page').start()

    def test_rotating_banner_cannot_block_webview_exit(self):
        # A coordinate close was rejected even though only an ad below it changed.
        def coordinate_tap(result, *args):
            if result['page'] == 'library_webview':
                raise m.ActionNotSubmitted('rotating banner overlaps old target patch')
            return {'ok': True}
        with patch.object(m, 'inspect', side_effect=[route('library_webview'), route('official_conversation')]), patch.object(m, 'tap_visual', side_effect=coordinate_tap):
            m.refresh_seat_page(self.folder, 1440, 3120)
        self.assertEqual(self.actions, [{'snapshotId': 'fresh-native',
            'action': {'type': 'global_action', 'name': 'back'}}])
        self.scroll.assert_called_once()

    def test_each_back_requires_fresh_route_not_assumed_destination(self):
        pages = [route('library_webview'), route('library_webview'), route('official_conversation')]
        with patch.object(m, 'inspect', side_effect=pages), patch.object(m, 'tap_visual') as tap:
            m.refresh_seat_page(self.folder, 1440, 3120)
        self.assertEqual(len(self.actions), 2)
        tap.assert_called_once()

    def test_other_foreground_app_is_never_backed(self):
        with patch.object(m, 'inspect', return_value=route('library_webview')), patch.object(m, 'observe', return_value={'package': 'other.app', 'snapshotId': 'other'}), patch.object(m, 'tap_visual') as tap:
            with self.assertRaises(m.RestartFlow):
                m.refresh_seat_page(self.folder, 1440, 3120)
        self.assertEqual(self.actions, [])
        tap.assert_not_called()

    def test_low_confidence_does_not_authorize_native_back(self):
        with patch.object(m, 'inspect', return_value=route('library_webview', confidence=.6)), patch.object(m, 'tap_visual') as tap:
            with self.assertRaises(m.RestartFlow):
                m.refresh_seat_page(self.folder, 1440, 3120)
        self.assertEqual(self.actions, [])
        tap.assert_not_called()

    def test_invalid_route_vote_does_not_authorize_back(self):
        for changes in ({'confidence': float('nan')}, {'confidence': float('inf')},
                        {'confidence': True}, {'target_found': 'true'}):
            self.actions.clear()
            with self.subTest(changes=changes), patch.object(m, 'inspect', return_value=route('library_webview', **changes)), patch.object(m, 'tap_visual'):
                with self.assertRaises(m.RestartFlow):
                    m.refresh_seat_page(self.folder, 1440, 3120)
                self.assertEqual(self.actions, [])

    def test_popup_is_confirmed_before_any_back(self):
        pages = [route('login_popup', target_found=False, transient_auth_popup=True), route('official_conversation')]
        with patch.object(m, 'inspect', side_effect=pages), patch.object(m, 'tap_visual'):
            m.refresh_seat_page(self.folder, 1440, 3120)
        self.assertEqual(self.actions, [])
        self.assertEqual(self.dismiss.call_count, 2)

    def test_unknown_native_outcome_is_not_replayed(self):
        calls = []
        def unknown(command, params, **kwargs):
            if command == 'mobile.ui.observe':
                return {'package': m.WECHAT, 'snapshotId': 'fresh-native'}
            calls.append(params)
            raise TimeoutError('unknown Back outcome')
        with patch.object(m, 'inspect', return_value=route('library_webview')), patch.object(m, 'invoke', side_effect=unknown), patch.object(m, 'tap_visual'):
            with self.assertRaises(TimeoutError):
                m.refresh_seat_page(self.folder, 1440, 3120)
        self.assertEqual(len(calls), 1)

    def test_stale_native_snapshot_requires_new_vision(self):
        count = 0
        def stale(command, params, **kwargs):
            nonlocal count
            if command == 'mobile.ui.observe':
                return {'package': m.WECHAT, 'snapshotId': 'fresh-native'}
            count += 1
            return {'code': 'target_stale'}
        with patch.object(m, 'inspect', side_effect=[route('library_webview'), route('official_conversation')]) as see, patch.object(m, 'invoke', side_effect=stale), patch.object(m, 'tap_visual'):
            m.refresh_seat_page(self.folder, 1440, 3120)
        self.assertEqual(count, 1)
        self.assertEqual(see.call_count, 2)

    def test_back_navigation_has_bounded_attempts(self):
        with patch.object(m, 'inspect', return_value=route('library_webview')), patch.object(m, 'tap_visual'):
            with self.assertRaises(m.RestartFlow):
                m.refresh_seat_page(self.folder, 1440, 3120)
        self.assertEqual(len(self.actions), 6)
        self.scroll.assert_not_called()

    def test_each_rejected_step_records_specific_reason(self):
        with patch.object(m, 'inspect', side_effect=[route('library_webview'), route('official_conversation')]), patch.object(m, 'act', side_effect=m.ActionNotSubmitted('native epoch changed')), patch.object(m, 'tap_visual'):
            m.refresh_seat_page(self.folder, 1440, 3120)
        data = json.loads((self.folder/'seat-refresh-navigation.json').read_text())
        self.assertEqual(data['steps'][0]['outcome'], 'not_submitted')
        self.assertIn('native epoch changed', data['steps'][0]['reason'])
        self.assertEqual(data['steps'][1]['route'], 'official_conversation')


if __name__ == '__main__':
    unittest.main()
