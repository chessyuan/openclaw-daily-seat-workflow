"""A second blank wallpaper region must not substitute for candidate validation."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('m', '/tmp/android-signin-candidate-1106-cleanup.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class WallpaperCandidateTests(unittest.TestCase):
    def setUp(self):
        m.RUN_CONTEXT.clear()
        m.BUSINESS_DEADLINE = None
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        (self.folder/'screen-off.jpg').write_bytes(b'jpeg-test-only')
        self.addCleanup(patch.stopall)
        patch.object(m.time, 'sleep').start()
        patch.object(m, 'observe', return_value={'package': 'com.sec.android.app.launcher', 'nodes': []}).start()
        patch.object(m, 'fast_coordinate').start()
        self.point = {'target_found': True, 'x_norm': 220, 'y_norm': 825, 'confidence': .99,
                      'evidence': 'Safe blank region', '_visualSnapshotId': 'frame',
                      '_visualImagePath': str(self.folder/'screen-off.jpg')}
        patch.object(m, 'inspect', return_value=(self.point, 945, 2048)).start()
        self.tap = patch.object(m, 'tap_visual').start()

    def test_same_candidate_is_independently_verified_before_double_tap(self):
        review = dict(candidate_safe=True, target_found=True, x_norm=220, y_norm=825, confidence=.99)
        with patch.object(m, 'vision', return_value=review) as verify:
            result = m.turn_screen_off(self.folder, 1440, 3120)
            verify.assert_called_once()
            self.assertIn('220', verify.call_args.args[2])
            self.assertIn('825', verify.call_args.args[2])
        self.assertTrue(result['requested'])
        self.tap.assert_called_once()

    def test_another_valid_blank_point_does_not_authorize_this_candidate(self):
        review = dict(candidate_safe=False, target_found=True, x_norm=100, y_norm=820, confidence=.99)
        with patch.object(m, 'vision', return_value=review):
            with self.assertRaises(m.FlowError):
                m.turn_screen_off(self.folder, 1440, 3120)
        self.tap.assert_not_called()

    def test_percentage_confidence_is_normalized_for_candidate_review(self):
        self.point['confidence'] = 98
        review = dict(candidate_safe=True, target_found=True, x_norm=220, y_norm=825, confidence=99)
        with patch.object(m, 'vision', return_value=review):
            m.turn_screen_off(self.folder, 1440, 3120)
        confidence = self.tap.call_args.args[0]['confidence']
        self.assertGreaterEqual(confidence, .9)
        self.assertLessEqual(confidence, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
