import importlib.util, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

s=importlib.util.spec_from_file_location('m','/tmp/android-signin-1023-guarded.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)

def nav(text='选座 >', **kw):
    return dict(action='navigate',target_text=text,target_found=True,confidence=.99,
                page='首页',_visualSnapshotId='old-navigation-frame',**kw)

def favorite(**kw):
    d=dict(target_found=True,success=False,confidence=.99,selection='primary',
           selected_seat='博学楼四楼半圆弧平台 7号',_visualSnapshotId='fresh-booking-frame')
    d.update(kw);return d

class Tests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.addCleanup(patch.stopall)
        patch.object(m,'ROOT',self.root).start();patch.object(m.time,'sleep').start()
        m.RUN_CONTEXT.clear();m.BUSINESS_DEADLINE=m.SETTLEMENT_DEADLINE=m.RESERVATION_DEADLINE=m.BOOKING_OPENS=None
        for name in ('invoke','act','fast_coordinate','provider'):
            patch.object(m,name,side_effect=AssertionError('No external action: '+name)).start()

    def live(self, results):
        with patch.object(m,'inspect',side_effect=[(r,945,2048) for r in results]):
            return m.verify_live_seats(self.root,1440,3120,None)

    def test_bare_select_cannot_be_navigation(self): self.assertTrue(m.ambiguous_seat_navigation(nav('选座')))
    def test_alternate_target_text_field_cannot_bypass_guard(self):
        self.assertTrue(m.ambiguous_seat_navigation(nav('',confirm_text='选座 >')))
    def test_arrow_and_spacing_variants_cannot_be_navigation(self):
        for text in ('选座 >','选座›','选座》','选座 →','选 座 >'):
            with self.subTest(text=text): self.assertTrue(m.ambiguous_seat_navigation(nav(text)))
    def test_authorized_card_name_cannot_be_navigation(self):
        self.assertTrue(m.ambiguous_seat_navigation(nav('博学楼四楼半圆弧平台7号')))
    def test_seat_identity_in_nav_forces_reverification(self):
        self.assertTrue(m.ambiguous_seat_navigation(nav('入口',selected_seat='博学楼三楼半圆弧平7号')))
    def test_explicit_list_and_tab_are_navigation(self):
        for text in ('去选座','底部选座','座位','关闭'):
            self.assertFalse(m.ambiguous_seat_navigation(nav(text)))
    def test_ambiguous_select_returns_fresh_booking_not_tap(self):
        with patch.object(m,'tap_visual') as tap:
            out=self.live([nav(),favorite()]);self.assertEqual(out[0]['_visualSnapshotId'],'fresh-booking-frame')
            tap.assert_not_called();self.assertIsNone(m.pending_reservation())
    def test_backup_identity_is_preserved(self):
        r=favorite(selection='backup',selected_seat='博学楼三楼半圆弧平7号')
        with patch.object(m,'tap_visual') as tap:
            self.assertEqual(self.live([nav(),r])[0]['selection'],'backup');tap.assert_not_called()
    def test_wrong_seat_rejected_without_tap(self):
        bad=favorite(selected_seat='博学楼四楼半圆弧平8号')
        owned=dict(action='owned',success=True,target_found=False)
        with patch.object(m,'tap_visual') as tap:
            self.live([nav(),bad,owned]);tap.assert_not_called()
    def test_selection_identity_mismatch_rejected(self):
        owned=dict(action='owned',success=True,target_found=False)
        with patch.object(m,'tap_visual') as tap:
            self.live([nav(),favorite(selection='backup'),owned]);tap.assert_not_called()
    def test_low_confidence_no_click(self):
        owned=dict(action='owned',success=True,target_found=False)
        with patch.object(m,'tap_visual') as tap:
            self.live([nav(),favorite(confidence=.5),owned]);tap.assert_not_called()
    def test_loading_target_no_click(self):
        owned=dict(action='owned',success=True,target_found=False)
        with patch.object(m,'tap_visual') as tap:
            self.live([nav(),favorite(loading=True),owned]);tap.assert_not_called()
    def test_no_target_does_not_reuse_old_frame(self):
        owned=dict(action='owned',success=True,target_found=False)
        with patch.object(m,'tap_visual') as tap:
            self.live([nav(),favorite(target_found=False),owned]);tap.assert_not_called()
    def test_owned_after_reverification_skips_booking(self):
        with patch.object(m,'tap_visual') as tap:
            out=self.live([nav(),favorite(target_found=False,success=True,selection='owned')])
            self.assertTrue(out[0]['success']);tap.assert_not_called()
    def test_popup_dismissed_instead_of_background_booking(self):
        owned=dict(action='owned',success=True,target_found=False)
        with patch.object(m,'tap_visual') as tap,patch.object(m,'dismiss_school_binding_popup') as dismiss:
            self.live([nav(),favorite(transient_auth_popup=True),owned])
            dismiss.assert_called_once();tap.assert_not_called()
    def test_unknown_reverification_stops_after_ten_observations(self):
        with patch.object(m,'tap_visual') as tap:
            with self.assertRaises(m.RestartFlow):self.live([x for _ in range(10) for x in (nav(),favorite(target_found=False))])
            tap.assert_not_called()
    def test_model_failure_before_booking_leaves_no_pending(self):
        with patch.object(m,'inspect',side_effect=[(nav(),945,2048),m.VisionProviderError('timeout')]),patch.object(m,'tap_visual') as tap:
            with self.assertRaises(m.VisionProviderError):m.verify_live_seats(self.root,1440,3120,None)
            tap.assert_not_called();self.assertIsNone(m.pending_reservation())
    def test_ready_still_uses_outer_submit(self):
        with patch.object(m,'tap_visual') as tap:
            out=self.live([dict(favorite(),action='ready')]);self.assertTrue(out[0]['target_found']);tap.assert_not_called()
    def test_navigation_to_list_is_still_allowed(self):
        with patch.object(m,'tap_visual') as tap:
            self.live([nav('去选座'),dict(action='owned',success=True,target_found=False)])
            tap.assert_called_once()
    def test_closing_ad_is_navigation_not_booking(self):
        with patch.object(m,'tap_visual') as tap:
            self.live([nav('关闭'),dict(action='owned',success=True,target_found=False)])
            tap.assert_called_once();self.assertIsNone(m.pending_reservation())
    def test_guarded_target_journal_precedes_click_and_settles_immediately(self):
        out=self.live([nav(),favorite()]);events=[]
        def click(target,*args):
            pending=m.pending_reservation();self.assertEqual(pending['status'],'submission_uncertain')
            self.assertEqual(target['_visualSnapshotId'],'fresh-booking-frame');events.append('click')
        def settle(folder):events.append('settle');return {'success':True}
        with patch.object(m,'tap_visual',side_effect=click),patch.object(m,'settle_pending',side_effect=settle):
            self.assertTrue(m.submit_and_settle(out,self.root,1440,3120,None)['success'])
        self.assertEqual(events,['click','settle'])
    def test_ambiguous_tap_timeout_preserves_pending(self):
        out=self.live([nav(),favorite()])
        with patch.object(m,'tap_visual',side_effect=TimeoutError),patch.object(m,'settle_pending') as settle:
            with self.assertRaises(TimeoutError):m.submit_and_settle(out,self.root,1440,3120,None)
            self.assertEqual(m.pending_reservation()['status'],'submission_uncertain');settle.assert_not_called()
    def test_popup_state_prompt_distinguishes_no_target_from_failure(self):
        with patch.object(m,'inspect',return_value=({'success':True,'target_found':False},945,2048)) as observe:
            m.dismiss_school_binding_popup(self.root,'popup',1440,3120)
            self.assertIn('没有按钮需要点击，不是失败',observe.call_args.args[0]);observe.assert_called_once()

if __name__=='__main__':unittest.main()
