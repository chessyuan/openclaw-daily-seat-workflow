import importlib.util,json,subprocess,tempfile,unittest
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
s=importlib.util.spec_from_file_location('m','/tmp/android-signin-audit.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)

def found(**kw):
    return dict(target_found=True,confidence=.95,x_norm=500,y_norm=500,page='首页',success=False,**kw)

class Fixes1010(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        m.BOOKING_OPENS=m.BUSINESS_DEADLINE=m.SETTLEMENT_DEADLINE=None

    def test_structured_accessibility_error_not_help(self):
        p=subprocess.CompletedProcess([],1,json.dumps({'ok':False,'error':{'message':'SERVICE_DISABLED: Accessibility disabled'}}),'[openclaw] Help: openclaw --help')
        with patch.object(m.subprocess,'run',return_value=p):
            with self.assertRaisesRegex(m.FlowError,'SERVICE_DISABLED'):m.run_json([])

    def test_navigation_stale_reobserves_locally(self):
        with patch.object(m,'tap_visual',side_effect=[m.ActionNotSubmitted(),{}]) as tap,patch.object(m,'inspect',return_value=(found(),100,200)) as see,patch.object(m.time,'sleep'):
            m.tap_navigation((found(),100,200),'只找座位',self.root,'nav',100,200)
            self.assertEqual(tap.call_count,2);see.assert_called_once()

    def test_navigation_three_rejections_stop(self):
        with patch.object(m,'tap_visual',side_effect=m.ActionNotSubmitted()) as tap,patch.object(m,'inspect',return_value=(found(),100,200)),patch.object(m.time,'sleep'):
            with self.assertRaises(m.RestartFlow):m.tap_navigation((found(),100,200),'座位',self.root,'nav',100,200)
            self.assertEqual(tap.call_count,3)

    def test_navigation_unknown_timeout_not_replayed(self):
        with patch.object(m,'tap_visual',side_effect=TimeoutError()) as tap:
            with self.assertRaises(TimeoutError):m.tap_navigation((found(),100,200),'座位',self.root,'nav',100,200)
            tap.assert_called_once()

    def test_popup_stale_is_local_reobserve_not_full_restart(self):
        popup=found(transient_auth_popup=True,dialog_text='尚未登录',confirm_text='确定')
        done={'target_found':False,'success':True,'loading':False,'transient_auth_popup':False,'school_binding_popup':False}
        with patch.object(m,'inspect',side_effect=[(popup,100,200),(popup,100,200),(done,100,200)]) as see,patch.object(m,'tap_visual',side_effect=[m.ActionNotSubmitted(),{}]) as tap,patch.object(m.time,'sleep'):
            result=m.dismiss_school_binding_popup(self.root,'popup',100,200)
            self.assertTrue(result['handled']);self.assertEqual(see.call_count,3);self.assertEqual(tap.call_count,2)

    def test_opening_forces_reload_after_wait(self):
        m.BOOKING_OPENS=m.datetime.now(m.TZ)+timedelta(minutes=1)
        events=[]
        with patch.object(m,'wait_for_opening',side_effect=lambda d:events.append('wait')),patch.object(m,'refresh_seat_page',side_effect=lambda *a,**k:events.append('reload')):
            m.wait_and_refresh_opening(self.root,100,200,None)
        self.assertEqual(events,['wait','reload'])

    def test_late_run_does_not_reload_unnecessarily(self):
        m.BOOKING_OPENS=m.datetime.now(m.TZ)-timedelta(minutes=1)
        with patch.object(m,'wait_for_opening'),patch.object(m,'refresh_seat_page') as refresh:
            m.wait_and_refresh_opening(self.root,100,200,None)
            refresh.assert_not_called()

    def test_closed_favorites_refresh_before_live_route(self):
        empty={'target_found':False,'success':False,'evidence':'水印已被预订'}
        with patch.object(m,'inspect',return_value=(empty,100,200)),patch.object(m,'refresh_seat_page') as refresh,patch.object(m,'verify_live_seats',return_value=(found(),100,200)) as route,patch.object(m.time,'sleep'):
            m.wait_target('目标',self.root,'favorite',100,200,checks=3)
            refresh.assert_called_once();route.assert_called_once()

    def test_live_review_same_named_target_not_other_entry(self):
        planned=found(action='navigate',target_text='去选座',evidence='首页按钮')
        prompts=[]
        def fake(raw,mime,instruction):
            prompts.append(instruction);return dict(planned)
        with patch.object(m,'screenshot',return_value=(b'img','image/jpeg',100,200)),patch.object(m,'vision',side_effect=fake):
            result,_,_=m.inspect('目标',self.root,'live-seat-0',consensus=True)
        self.assertTrue(result['target_found']);self.assertIn('去选座',prompts[1]);self.assertIn('禁止改选另一个入口',prompts[1]);self.assertNotIn('x_norm',prompts[1])

    def test_live_review_different_route_cannot_click(self):
        a=found(action='navigate',target_text='去选座')
        b=found(action='navigate',target_text='选座')
        with patch.object(m,'screenshot',return_value=(b'img','image/jpeg',100,200)),patch.object(m,'vision',side_effect=[a,b]):
            result,_,_=m.inspect('目标',self.root,'live-seat-0',consensus=True)
        self.assertFalse(result['target_found']);self.assertEqual(result['action'],'wait')

    def test_live_disagreement_reobserves_no_app_reset(self):
        a={'action':'navigate','target_text':'去选座','target_found':False,'confidence':0}
        b=found(action='ready',selected_seat='博学楼四楼半圆弧平7号',selection='primary')
        with patch.object(m,'inspect',side_effect=[(a,100,200),(b,100,200)]),patch.object(m,'tap_visual') as tap,patch.object(m.time,'sleep'):
            result,_,_=m.verify_live_seats(self.root,100,200,None)
        self.assertEqual(result['selection'],'primary');tap.assert_not_called()

    def test_scroll_stale_fresh_page_before_retry(self):
        with patch.object(m,'fast_coordinate',side_effect=[m.ActionNotSubmitted(),{}]) as scroll,patch.object(m,'inspect',return_value=({'success':True},100,200)) as see,patch.object(m.time,'sleep'):
            m.scroll_seat_page(100,200,None,self.root,'scroll')
        self.assertEqual(scroll.call_count,2);see.assert_called_once()

    def test_scroll_changed_page_does_not_replay(self):
        with patch.object(m,'fast_coordinate',side_effect=m.ActionNotSubmitted()) as scroll,patch.object(m,'inspect',return_value=({'success':False},100,200)),patch.object(m.time,'sleep'):
            with self.assertRaises(m.RestartFlow):m.scroll_seat_page(100,200,None,self.root,'scroll')
            scroll.assert_called_once()

    def test_exact_seat_alias_not_other_seats(self):
        self.assertEqual(m.canonical_seat_name('博学楼四楼半圆弧平台场馆 7 号(4)'),'博学楼四楼半圆弧平7号')
        self.assertNotEqual(m.canonical_seat_name('博学楼四楼半圆弧平台场馆 8号(4)'),'博学楼四楼半圆弧平7号')

    def test_whole_favorite_card_cannot_bypass_submission_record(self):
        card=found(action='navigate',target_text='博学楼四楼半圆弧平7号')
        ready=found(action='ready',selected_seat='博学楼四楼半圆弧平7号',selection='primary')
        with patch.object(m,'inspect',side_effect=[(card,100,200),(ready,100,200)]),patch.object(m,'tap_visual') as tap,patch.object(m.time,'sleep'):
            result,_,_=m.verify_live_seats(self.root,100,200,None)
            self.assertEqual(result['selection'],'primary');tap.assert_not_called()

    def test_run_once_owned_reservation_never_submits(self):
        owned={'target_found':False,'success':True,'selected_seat':'博学楼四楼半圆弧平7号','selection':'owned'}
        with patch.object(m,'prepare_display',return_value=(100,200)),patch.object(m,'inspect',return_value=(found(),100,200)),patch.object(m,'tap_navigation'),patch.object(m,'wait_target',side_effect=[(found(),100,200),(owned,100,200)]),patch.object(m,'dismiss_school_binding_popup'),patch.object(m,'scroll_seat_page'),patch.object(m,'wait_and_refresh_opening'),patch.object(m,'save_pending'),patch.object(m,'settle_pending',return_value={}) as settle,patch.object(m,'submit_and_settle') as submit,patch.object(m.time,'sleep'):
            m.run_once(self.root)
            settle.assert_called_once();submit.assert_not_called()

unittest.main()
