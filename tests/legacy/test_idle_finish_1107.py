"""User policy: business completion never requires an active screen-off gesture."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('m', '/tmp/android-signin-candidate-1106-cleanup.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class IdleFinishTests(unittest.TestCase):
    def setUp(self):
        m.RUN_CONTEXT.clear()
        m.BUSINESS_DEADLINE = m.BOOKING_OPENS = m.SETTLEMENT_DEADLINE = m.RESERVATION_DEADLINE = None
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.addCleanup(patch.stopall)
        patch.object(m, 'ROOT', self.root).start()

    def run_main(self, business, final_success):
        with (patch.object(m.sys, 'argv', ['signin', '--max-attempts', '1', '--finish-phone']),
              patch.object(m, 'pending_reservation', return_value=None),
              patch.object(m, 'model_preflight'), patch.object(m, 'ensure_node'),
              patch.object(m, 'run_once', side_effect=business if isinstance(business, Exception) else None,
                           return_value=business if isinstance(business, dict) else None),
              patch.object(m, 'final_checkin_once', return_value={'success': final_success}),
              patch.object(m, 'diagnose_failures', return_value={}),
              patch.object(m, 'write_run'), patch.object(m, 'finish_with_budget', return_value={}) as finish):
            code = m.main()
            finish.assert_called_once()
            self.assertIs(finish.call_args.kwargs.get('power_off'), False,
                          'Formal workflow still requests active screen-off')
        return code

    def test_normal_success_uses_system_idle(self):
        self.assertEqual(self.run_main({'screen': [1440, 3120], 'wegolibrary': {'success': True}}, True), 0)

    def test_final_checkin_rescue_uses_system_idle(self):
        self.assertEqual(self.run_main(m.WindowClosed('stop'), True), 0)

    def test_failed_workflow_still_uses_system_idle(self):
        self.assertEqual(self.run_main(m.WindowClosed('stop'), False), 1)

    def test_idle_cleanup_returns_home_without_tapping_or_locking(self):
        with (patch.object(m, 'clear_recent_apps', return_value={'cleared': True}),
              patch.object(m, 'turn_screen_off', side_effect=AssertionError('No active screen-off')),
              patch.object(m, 'fast_coordinate') as action):
            result = m.best_effort_finish(self.root, 1440, 3120, power_off=False)
        action.assert_called_once_with('global', 'home')
        self.assertFalse(result['screenOff']['requested'])
        self.assertEqual(result['screenOff']['method'], 'system_idle_timeout')


if __name__ == '__main__':
    unittest.main(verbosity=2)
