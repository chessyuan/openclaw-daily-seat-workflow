import importlib.util,tempfile,unittest,json,subprocess
from pathlib import Path
from unittest.mock import patch
s=importlib.util.spec_from_file_location('m','/tmp/android-signin-audit.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class StaleTests(unittest.TestCase):
 def target(self):return {'target_found':True,'selected_seat':'博学楼四楼半圆弧平7号','selection':'primary'}
 def test_structured_rejection(self):
  proc=subprocess.CompletedProcess([],1,json.dumps({'ok':False,'result':{'code':'target_stale'}}),'')
  with patch.object(m.subprocess,'run',return_value=proc):
   with self.assertRaises(m.ActionNotSubmitted):m.fast_coordinate('tap',10,10)
 def test_reobserve_then_submit(self):
  with tempfile.TemporaryDirectory() as d,patch.object(m,'ROOT',Path(d)),patch.object(m,'tap_visual',side_effect=[m.ActionNotSubmitted(),None]) as tap,patch.object(m,'dismiss_school_binding_popup'),patch.object(m,'wait_target',return_value=(self.target(),100,200)) as observe,patch.object(m,'settle_pending',return_value={}) as settle,patch.object(m.time,'sleep'):
   m.submit_and_settle((self.target(),100,200),Path(d),100,200,None)
   self.assertEqual(tap.call_count,2);observe.assert_called_once();settle.assert_called_once()
 def test_three_rejections_no_pending(self):
  with tempfile.TemporaryDirectory() as d,patch.object(m,'ROOT',Path(d)),patch.object(m,'tap_visual',side_effect=m.ActionNotSubmitted()),patch.object(m,'dismiss_school_binding_popup'),patch.object(m,'wait_target',return_value=(self.target(),100,200)),patch.object(m,'settle_pending') as settle:
   with self.assertRaises(m.RestartFlow):m.submit_and_settle((self.target(),100,200),Path(d),100,200,None)
   self.assertIsNone(m.pending_reservation());settle.assert_not_called()
 def test_unknown_timeout_preserved(self):
  with tempfile.TemporaryDirectory() as d,patch.object(m,'ROOT',Path(d)),patch.object(m,'tap_visual',side_effect=TimeoutError()):
   with self.assertRaises(TimeoutError):m.submit_and_settle((self.target(),100,200),Path(d),100,200,None)
   self.assertEqual(m.pending_reservation()['status'],'submission_uncertain')
unittest.main()
