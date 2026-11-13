"""Regression: empty Android UI trees must not silently skip final cleanup."""
import importlib.machinery
import importlib.util
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

source = Path(sys.argv.pop(1)) if len(sys.argv) > 1 else Path('/tmp/android-signin-candidate-1106-cleanup.py')
loader = importlib.machinery.SourceFileLoader('m', str(source))
spec = importlib.util.spec_from_loader(loader.name, loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)
real_clear_recent_apps = m.clear_recent_apps


class EmptyTreeCleanupTests(unittest.TestCase):
    def setUp(self):
        m.RUN_CONTEXT.clear()
        m.BUSINESS_DEADLINE = None
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.addCleanup(patch.stopall)
        patch.object(m, 'observe', return_value={'package': 'com.tencent.mm', 'nodes': []}).start()
        for name in ('invoke', 'run_json', 'fast_coordinate', '_wegolibrary_request'):
            patch.object(m, name, side_effect=AssertionError('No outbound calls in regression: '+name)).start()
        self.clear = patch.object(m, 'clear_recent_apps', return_value={'cleared': True}).start()
        self.off = patch.object(m, 'turn_screen_off', return_value={'requested': True}).start()

    def assert_finished(self, result):
        self.assertTrue(result.get('cleanup', {}).get('cleared'), 'Empty UI tree silently skipped cleanup')
        self.assertTrue(result.get('screenOff', {}).get('requested'), 'Empty UI tree silently skipped screen-off')
        self.assertEqual(self.clear.call_args.args[1:3], (1440, 3120))
        self.assertEqual(self.off.call_args.args[1:3], (1440, 3120))

    def test_empty_tree_uses_geometry_measured_during_this_run(self):
        m.RUN_CONTEXT['displayGeometry'] = {'width': 1440, 'height': 3120}
        with patch.object(m, 'measure_current_display', side_effect=AssertionError('Already calibrated')):
            self.assert_finished(m.finish_with_budget(self.folder))

    def test_empty_tree_without_cache_calibrates_physical_coordinate_space(self):
        with patch.object(m, 'measure_current_display', return_value=(1440, 3120)) as measure:
            self.assert_finished(m.finish_with_budget(self.folder))
            measure.assert_called_once()

    def test_failed_calibration_is_reported_without_guessing_screen_size(self):
        with patch.object(m, 'measure_current_display', side_effect=m.DisplayGeometryChanged('cannot calibrate')):
            result = m.finish_with_budget(self.folder)
            self.assertIn('cannot calibrate', result.get('cleanupError', ''))
            self.clear.assert_not_called()
            self.off.assert_not_called()

    def test_explicit_physical_dimensions_preserve_success_path(self):
        with patch.object(m, 'measure_current_display', side_effect=AssertionError('Already supplied')):
            self.assert_finished(m.finish_with_budget(self.folder, 1440, 3120))

    def test_calibration_does_not_consume_the_recents_action_budget(self):
        real_datetime = m.datetime
        now = [real_datetime.now(m.TZ)]

        class Clock(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return now[0]

        def calibrate():
            now[0] += timedelta(seconds=20)
            return 1440, 3120

        budgets = []

        def cleanup(*args, **kwargs):
            budgets.append((m.BUSINESS_DEADLINE-now[0]).total_seconds())
            return {'cleared': True}

        with patch.object(m, 'datetime', Clock), patch.object(m, 'measure_current_display', side_effect=calibrate):
            self.clear.side_effect = cleanup
            self.assert_finished(m.finish_with_budget(self.folder))
        self.assertGreaterEqual(budgets[0], 35, 'Calibration consumed the recents action budget')

    def test_recents_navigation_stabilizes_launcher_before_opening_task_switcher(self):
        state = {'package': 'com.tencent.mm'}
        actions = []

        def observe():
            return {'package': state['package'], 'snapshotId': 'current', 'nodes': []}

        def action(snapshot, value):
            name = value['name']
            actions.append(name)
            if name == 'home':
                state['package'] = 'com.sec.android.app.launcher'
            elif name == 'recents' and state['package'] == 'com.sec.android.app.launcher':
                state['package'] = 'recents'
            return {'code': 'completed'}

        def inspect(*args, **kwargs):
            return ({'target_found': state['package'] == 'recents', 'confidence': .99,
                     'success': False, 'page': state['package'], 'loading': False}, 945, 2048)

        with (patch.object(m, 'observe', side_effect=observe), patch.object(m, 'act', side_effect=action),
              patch.object(m, 'inspect', side_effect=inspect), patch.object(m, 'tap_visual'),
              patch.object(m.time, 'sleep')):
            result = real_clear_recent_apps(self.folder, 1440, 3120)
        self.assertTrue(result.get('cleared'))
        self.assertEqual(actions[:2], ['home', 'recents'])

    def test_uncalibrated_cleanup_reserves_time_for_independent_vision_and_fresh_frame(self):
        real_datetime = m.datetime
        now = [real_datetime.now(m.TZ)]

        class Clock(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return now[0]

        def calibrate():
            now[0] += timedelta(seconds=20)
            return 1440, 3120

        def clear(*args, **kwargs):
            now[0] += timedelta(seconds=35)
            return {'cleared': True}

        remaining = []

        def off(*args, **kwargs):
            remaining.append((m.BUSINESS_DEADLINE-now[0]).total_seconds())
            return {'requested': True}

        with patch.object(m, 'datetime', Clock), patch.object(m, 'measure_current_display', side_effect=calibrate):
            self.clear.side_effect = clear
            self.off.side_effect = off
            self.assert_finished(m.finish_with_budget(self.folder))
        self.assertGreaterEqual(remaining[0], 45, 'No time left to verify the candidate and capture a fresh frame')


if __name__ == '__main__':
    unittest.main(verbosity=2)
