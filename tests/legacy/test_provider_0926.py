import importlib.util,io,unittest,urllib.error
from unittest.mock import patch
s=importlib.util.spec_from_file_location('s','/tmp/android-signin-audit.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class ProviderTests(unittest.TestCase):
    def test_validation_classified_without_raw_secret(self):
        e=urllib.error.HTTPError('http://local',403,'error',{},io.BytesIO(b'{"reason":"VALIDATION_REQUIRED","secret":"do-not-print"}'))
        out=str(m.provider_error(e));self.assertIn('VALIDATION_REQUIRED',out);self.assertNotIn('do-not-print',out)
    def test_http_failure_does_not_repeat_request(self):
        e=urllib.error.HTTPError('http://local',503,'error',{},io.BytesIO(b'auth unavailable'))
        with patch.object(m,'provider',return_value=('http://local','fake')),patch.object(m.urllib.request,'urlopen',side_effect=e) as call:
            with self.assertRaises(m.VisionProviderError):m.vision(b'image','image/jpeg','test')
            self.assertEqual(call.call_count,1)
    def test_preflight_no_phone_action(self):
        with patch.object(m,'provider',return_value=('http://local','fake')),patch.object(m.urllib.request,'urlopen',side_effect=TimeoutError),patch.object(m,'ensure_node') as phone:
            with self.assertRaises(m.VisionProviderError):m.model_preflight()
            phone.assert_not_called()
unittest.main()
