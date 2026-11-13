import importlib.util, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from datetime import timedelta
spec=importlib.util.spec_from_file_location('signin','/tmp/android-signin-audit.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
class FinalCheckin(unittest.TestCase):
    def test_expired_deadline_still_calls_once(self):
        with tempfile.TemporaryDirectory() as d, patch.object(m,'pending_reservation',return_value=None), patch.object(m,'complete_wegolibrary_checkin',return_value={'success':True,'message':'ok'}) as api:
            old=m.datetime.now(m.TZ)-timedelta(minutes=1); m.BUSINESS_DEADLINE=old
            r=m.final_checkin_once(Path(d))
            api.assert_called_once_with(force=True,require_new_timestamp=False)
            self.assertTrue(r['success']); self.assertEqual(m.BUSINESS_DEADLINE,old)
    def test_failure_no_retry(self):
        with tempfile.TemporaryDirectory() as d, patch.object(m,'complete_wegolibrary_checkin',side_effect=RuntimeError('offline')) as api:
            r=m.final_checkin_once(Path(d))
            self.assertFalse(r['success']); self.assertEqual(api.call_count,1)
            self.assertTrue((Path(d)/'final-checkin.json').exists())
    def test_pending_settled(self):
        with tempfile.TemporaryDirectory() as d, patch.object(m,'pending_reservation',return_value={'date':m.datetime.now(m.TZ).strftime('%Y-%m-%d'),'status':'submission_uncertain'}), patch.object(m,'save_pending') as save, patch.object(m,'complete_wegolibrary_checkin',return_value={'success':True}):
            m.final_checkin_once(Path(d))
            self.assertEqual(save.call_args.args[0]['status'],'settled')
unittest.main()
