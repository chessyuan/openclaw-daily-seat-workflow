import importlib.util, tempfile, unittest, io, json
from pathlib import Path
from unittest.mock import patch
from PIL import Image, ImageDraw
s=importlib.util.spec_from_file_location('m','/tmp/android-signin-1022-speed.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)

def picture(ad='blue',outside=False,target=False,modal=False):
    im=Image.new('RGB',(945,2048),'white');d=ImageDraw.Draw(im)
    d.rectangle((47,220,895,397),fill=ad)
    d.rectangle((80,1380,250,1550),fill='blue' if not target else 'red')
    if outside:d.rectangle((50,600,900,780),fill='red')
    if modal:d.rectangle((50,800,900,1300),fill='gray')
    b=io.BytesIO();im.save(b,format='PNG');return b.getvalue()

def vote(**kw):
    v=dict(target_found=True,confidence=.99,rebind_safe=True,x_norm=175,y_norm=730,
           selection='primary',selected_seat='博学楼四楼半圆弧平7号',
           transient_auth_popup=False,school_binding_popup=False,
           advertising_rect_norm=[45,105,955,198])
    v.update(kw);return v

class Tests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name)
        self.addCleanup(patch.stopall);patch.object(m,'ROOT',self.root).start()
        m.RUN_CONTEXT.clear();m.BUSINESS_DEADLINE=None
        m.RUN_CONTEXT['displayGeometry']={'width':1440,'height':3120}
        m.RUN_CONTEXT['visualFrame']={'snapshotId':'fresh-after-inference'}
        self.path=self.root/'seat.jpg';self.before=picture();self.current=picture('red')
        self.path.write_bytes(self.before)
        self.target={**vote(), '_visualInstruction':'只选择首选座位',
            '_visualImagePath':str(self.path),'_visualSnapshotId':'old'}
        self.ai=patch.object(m,'vision',return_value=vote()).start()
        self.capture=patch.object(m,'screenshot',return_value=(self.current,'image/png',945,2048)).start()
        self.tap=patch.object(m,'fast_coordinate',side_effect=AssertionError('no device gestures')).start()
    def verify(self):
        return m.verify_advertising_rebind(self.target,self.before,self.current,945,2048)
    def denied(self):
        with self.assertRaises(m.ActionNotSubmitted):self.verify()
        self.tap.assert_not_called()
    def test_rotating_banner_was_false_stale(self):
        self.assertFalse(m.compare_visual_frames(self.before,self.current,175,730)['match'])
        self.assertTrue(m.compare_visual_frames(self.before,self.current,175,730,banner=[45,105,955,198])['match'])
    def test_two_confirmations_and_post_inference_capture_required(self):
        self.assertEqual(self.verify(),'fresh-after-inference');self.assertEqual(self.ai.call_count,2)
        self.capture.assert_called_once();self.tap.assert_not_called()
    def test_ad_can_rotate_again_after_inference(self):
        self.capture.return_value=(picture('green'),'image/png',945,2048)
        self.assertEqual(self.verify(),'fresh-after-inference')
    def test_other_page_change_denied(self):
        self.current=picture('red',outside=True);self.denied()
    def test_target_pixel_change_denied(self):
        self.current=picture('red',target=True);self.denied()
    def test_new_modal_after_inference_denied(self):
        self.capture.return_value=(picture('red',modal=True),'image/png',945,2048);self.denied()
    def test_target_moves_denied(self):
        self.ai.return_value=vote(x_norm=220);self.denied()
    def test_different_seat_denied(self):
        self.ai.return_value=vote(selected_seat='其它座位');self.denied()
    def test_different_branch_denied(self):
        self.ai.return_value=vote(selection='backup');self.denied()
    def test_one_unsafe_vote_denied(self):
        self.ai.side_effect=[vote(),vote(rebind_safe=False)];self.denied()
    def test_low_confidence_denied(self):
        self.ai.return_value=vote(confidence=.89);self.denied()
    def test_nan_confidence_denied(self):
        self.ai.return_value=vote(confidence=float('nan'));self.denied()
    def test_nan_coordinates_denied(self):
        self.ai.return_value=vote(x_norm=float('nan'));self.denied()
    def test_boolean_confidence_denied(self):
        self.ai.return_value=vote(confidence=True);self.denied()
    def test_new_popup_denied(self):
        self.ai.return_value=vote(transient_auth_popup=True);self.denied()
    def test_loading_denied(self):
        self.ai.return_value=vote(loading=True);self.denied()
    def test_missing_banner_denied(self):
        self.ai.return_value=vote(advertising_rect_norm=None);self.denied()
    def test_entire_screen_mask_denied(self):
        self.ai.return_value=vote(advertising_rect_norm=[0,0,1000,1000]);self.denied()
    def test_bottom_seat_mask_denied(self):
        self.ai.return_value=vote(advertising_rect_norm=[0,600,1000,800]);self.denied()
    def test_banner_must_not_touch_target_patch(self):
        self.target['y_norm']=195;self.ai.return_value=vote(y_norm=195);self.denied()
    def test_models_must_agree_on_banner_region(self):
        self.ai.side_effect=[vote(),vote(advertising_rect_norm=[45,250,955,300])];self.denied()
    def test_intersection_cannot_hide_extra_changes(self):
        self.ai.side_effect=[vote(),vote(advertising_rect_norm=[45,160,955,198])];self.denied()
    def test_no_intent_no_ai(self):
        self.target.pop('_visualInstruction');self.denied();self.ai.assert_not_called()
    def test_dimension_change_denied(self):
        self.capture.return_value=(self.current,'image/png',900,2048);self.denied()
    def test_missing_snapshot_denied(self):
        m.RUN_CONTEXT.pop('visualFrame');self.denied()
    def test_matching_frame_fast_path_does_not_call_ai(self):
        self.capture.return_value=(self.before,'image/png',945,2048)
        self.assertEqual(m.validate_visual_frame(self.target,945,2048),'fresh-after-inference')
        self.ai.assert_not_called()
    def test_validation_reaches_banner_recheck_without_device_tap(self):
        self.assertEqual(m.validate_visual_frame(self.target,945,2048),'fresh-after-inference')
        self.assertEqual(m.RUN_CONTEXT['lastFrameValidation']['reason'],'dual_verified_advertising_only')
        self.tap.assert_not_called()
    def test_popup_confirm_must_remain_identical(self):
        self.target.update(transient_auth_popup=True,dialog_text='尚未登录',confirm_text='确定')
        self.ai.return_value=vote(transient_auth_popup=True,dialog_text='确定删除',confirm_text='确定')
        self.denied()
    def test_changed_target_never_invokes_recheck(self):
        self.capture.return_value=(picture('red',target=True),'image/png',945,2048)
        with self.assertRaises(m.ActionNotSubmitted):m.validate_visual_frame(self.target,945,2048)
        self.ai.assert_not_called();self.tap.assert_not_called()

if __name__=='__main__':unittest.main()
