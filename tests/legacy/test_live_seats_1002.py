import importlib.util,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
s=importlib.util.spec_from_file_location('s','/tmp/android-signin-audit.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class LiveSeats(unittest.TestCase):
 def check(self,result):return patch.object(m,'inspect',return_value=(result,100,200))
 def test_authorized_ready(self):
  with tempfile.TemporaryDirectory() as d,self.check({'action':'ready','target_found':True,'selected_seat':'博学楼四楼半圆弧平台7号','selection':'primary'}),patch.object(m,'tap_visual') as tap:
   r=m.verify_live_seats(Path(d),100,200,None);self.assertEqual(r[0]['selection'],'primary');tap.assert_not_called()
 def test_other_seat_blocked(self):
  with tempfile.TemporaryDirectory() as d,self.check({'action':'ready','selected_seat':'博学楼四楼半圆弧平台8号','selection':'primary'}):
   with self.assertRaises(m.FlowError):m.verify_live_seats(Path(d),100,200,None)
 def test_badge_not_authoritative(self):
  with tempfile.TemporaryDirectory() as d,self.check({'action':'unavailable','verified_live':False}),patch.object(m.time,'sleep'),patch.object(m,'tap_visual') as tap:
   with self.assertRaises(m.RestartFlow):m.verify_live_seats(Path(d),100,200,None)
   tap.assert_not_called()
 def test_tomorrow_blocked(self):
  with tempfile.TemporaryDirectory() as d,self.check({'action':'navigate','target_text':'明日预约'}),patch.object(m,'tap_visual') as tap:
   with self.assertRaises(m.FlowError):m.verify_live_seats(Path(d),100,200,None)
   tap.assert_not_called()
unittest.main()
