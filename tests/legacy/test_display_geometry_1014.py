import importlib.util,unittest,tempfile,sys
from pathlib import Path
from unittest.mock import patch
s=importlib.util.spec_from_file_location('signin','/tmp/android-signin-candidate-1014.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
def snap(package='com.tencent.mm',bounds=None,child=False):
    nodes=[] if bounds is None else [{'ref':'n0','parentRef':None,'bounds':bounds}]
    if child:nodes.append({'ref':'n1','parentRef':'n0','bounds':[0,0,3000,7000]})
    return {'package':package,'snapshotId':'id','nodes':nodes}
shot=(b'pixels','image/jpeg',945,2048)
class GeometryTests(unittest.TestCase):
    def setUp(self):
        m.RUN_CONTEXT.clear();m.BUSINESS_DEADLINE=None
    def test_full_frame_ignores_offscreen_children(self):
        tree=snap(bounds=[0,0,1440,3120],child=True)
        self.assertEqual(m.bounds_max(tree),(3000,7000))
        self.assertEqual(m.full_display_bounds(tree,945,2048),(1440,3120))
    def test_content_frame_excluding_bars_is_rejected(self):
        self.assertIsNone(m.full_display_bounds(snap(bounds=[0,100,1440,3024]),945,2048))
    def test_incorrect_aspect_is_rejected(self):
        self.assertIsNone(m.full_display_bounds(snap(bounds=[0,0,1440,3420]),945,2048))
    def test_empty_wechat_tree_not_guessed(self):
        self.assertIsNone(m.full_display_bounds(snap(),945,2048))
    def test_resolution_scaling_1080p(self):
        self.assertEqual(m.full_display_bounds(snap(bounds=[0,0,1080,2340]),945,2048),(1080,2340))
    def test_resolution_scaling_720p(self):
        self.assertEqual(m.full_display_bounds(snap(bounds=[0,0,720,1560]),720,1560),(720,1560))
    def test_dictionary_bounds(self):
        self.assertEqual(m.full_display_bounds(snap(bounds={'left':0,'top':0,'right':1440,'bottom':3120}),945,2048),(1440,3120))
    def test_malformed_bounds(self):
        for b in ([0,0,'bad',3120],[],[0,0,-1,3120]):self.assertIsNone(m.full_display_bounds(snap(bounds=b),945,2048))
    def test_child_only_bounds_rejected(self):
        tree=snap(bounds=[0,0,1440,3120]);tree['nodes'][0]['parentRef']='parent'
        self.assertIsNone(m.full_display_bounds(tree,945,2048))
    def test_landscape_stops_before_tap(self):
        with self.assertRaises(m.DisplayGeometryChanged):m.remember_display(snap(),2048,945,(3120,1440),'test')
    def test_current_root_needs_no_navigation(self):
        with patch.object(m,'observe',return_value=snap(bounds=[0,0,1440,3120],child=True)),patch.object(m,'screenshot',return_value=shot),patch.object(m,'fast_coordinate') as tap:
            self.assertEqual(m.measure_current_display(),(1440,3120));tap.assert_not_called()
    def test_wechat_empty_tree_uses_system_shade_and_restores(self):
        with patch.object(m,'observe',side_effect=[snap(),snap('com.android.systemui',[0,0,1440,3120]),snap()]),patch.object(m,'screenshot',return_value=shot),patch.object(m,'fast_coordinate') as action,patch.object(m.time,'sleep'):
            self.assertEqual(m.measure_current_display(),(1440,3120))
            self.assertEqual(action.call_args_list[0].args,('global','notifications'))
            self.assertEqual(action.call_args_list[1].args,('global','back'))
    def test_failed_shade_is_closed_without_coordinate_tap(self):
        with patch.object(m,'observe',side_effect=[snap(),snap('com.android.systemui',[0,0,1440,3420])]),patch.object(m,'screenshot',return_value=shot),patch.object(m,'fast_coordinate') as action,patch.object(m.time,'sleep'):
            with self.assertRaises(m.DisplayGeometryChanged):m.measure_current_display()
            self.assertEqual(action.call_args_list[-1].args,('global','back'))
    def test_changed_page_requires_new_observation(self):
        with patch.object(m,'observe',side_effect=[snap(),snap('com.android.systemui',[0,0,1440,3120]),snap('other.app')]),patch.object(m,'screenshot',return_value=shot),patch.object(m,'fast_coordinate'),patch.object(m.time,'sleep'):
            with self.assertRaises(m.DisplayGeometryChanged):m.measure_current_display()
    def test_invalid_geometry_is_explicitly_unsubmitted(self):
        target={'target_found':True,'confidence':.95,'x_norm':500,'y_norm':500}
        with patch.object(m,'fast_coordinate') as tap:
            with self.assertRaises(m.DisplayGeometryChanged):m.tap_visual(target,945,2048,1440,3420)
            tap.assert_not_called();self.assertIn('displayGeometryMismatch',m.RUN_CONTEXT)
    def test_recalibrated_frame_used_by_later_clicks(self):
        m.remember_display(snap(bounds=[0,0,1440,3120]),945,2048,(1440,3120),'test')
        with patch.object(m,'fast_coordinate') as tap,patch.object(m,'validate_visual_frame',return_value='test-frame'):
            m.tap_visual({'target_found':True,'confidence':.95,'x_norm':500,'y_norm':500,'_visualSnapshotId':'test-frame'},945,2048,1440,3420)
            tap.assert_called_once_with('tap-bound','test-frame',720,1560)
    def test_navigation_recalibrates_and_relocalizes_only_once(self):
        with tempfile.TemporaryDirectory() as d,patch.object(m,'tap_visual',side_effect=[m.DisplayGeometryChanged('rejected'),{'ok':True}]) as tap,patch.object(m,'measure_current_display',return_value=(1440,3120)) as measure,patch.object(m,'inspect',return_value=({},945,2048)) as inspect,patch.object(m.time,'sleep'):
            m.tap_navigation(({},945,2048),'same goal',Path(d),'nav',1440,3420)
            measure.assert_called_once();inspect.assert_called_once();self.assertEqual(tap.call_args.args[-2:],(1440,3120))
    def test_unknown_timeout_does_not_replay(self):
        with patch.object(m,'tap_visual',side_effect=TimeoutError),patch.object(m,'measure_current_display') as measure:
            with self.assertRaises(TimeoutError):m.tap_navigation(({},945,2048),'goal',Path('/tmp'),'nav',1440,3120)
            measure.assert_not_called()
    def test_known_geometry_failure_stops_after_two_transactions(self):
        with tempfile.TemporaryDirectory() as d,patch.object(m,'ROOT',Path(d)),patch.object(m,'pending_reservation',return_value=None),patch.object(m,'model_preflight'),patch.object(m,'ensure_node'),patch.object(m,'run_once',side_effect=m.DisplayGeometryChanged('display invalid')) as run,patch.object(m,'diagnose_failures',return_value={}),patch.object(m,'write_run'),patch.object(m,'final_checkin_once') as final,patch.object(m,'best_effort_finish') as reset,patch.object(m.time,'sleep'),patch.object(sys,'argv',['android-signin','--until','23:59']):
            self.assertEqual(m.main(),1);self.assertEqual(run.call_count,2);reset.assert_not_called();final.assert_called_once()
    def test_landscape_launcher_recalibrates_after_portrait_wechat_launch(self):
        landscape=(b'pixels','image/jpeg',2048,945)
        with patch.object(m,'observe',side_effect=[snap(),snap('com.sec.android.app.launcher',[0,0,3120,1440])]),patch.object(m,'screenshot',side_effect=[shot,landscape,shot,shot]),patch.object(m,'invoke') as launch,patch.object(m,'act'),patch.object(m,'fast_coordinate'),patch.object(m,'measure_current_display',return_value=(1440,3120)) as measure,patch.object(m.time,'sleep'):
            self.assertEqual(m.prepare_display(),(1440,3120))
            self.assertEqual(launch.call_count,2);measure.assert_called_once()
    def test_portrait_launcher_rotation_after_launch_is_remeasured(self):
        changed=(b'pixels','image/jpeg',900,2048)
        with patch.object(m,'observe',side_effect=[snap(),snap('com.sec.android.app.launcher',[0,0,1440,3120])]),patch.object(m,'screenshot',side_effect=[shot,shot,changed,shot]),patch.object(m,'invoke'),patch.object(m,'act'),patch.object(m,'fast_coordinate'),patch.object(m,'measure_current_display',return_value=(1440,3120)) as measure,patch.object(m.time,'sleep'):
            self.assertEqual(m.prepare_display(),(1440,3120));measure.assert_called_once()
    def test_page_only_consensus_does_not_require_coordinates(self):
        state={'target_found':True,'success':True,'confidence':.96,'x_norm':None,'y_norm':None,'page':'homepage'}
        with tempfile.TemporaryDirectory() as d,patch.object(m,'screenshot',return_value=shot),patch.object(m,'vision',return_value=state):
            result,_,_=m.inspect('read page only',Path(d),'state',consensus=True,page_only=True)
            self.assertTrue(result['success']);self.assertFalse(result['target_found'])
    def test_page_only_disagreement_never_authorizes_success(self):
        state={'target_found':True,'success':True,'confidence':.96,'x_norm':None,'y_norm':None}
        with tempfile.TemporaryDirectory() as d,patch.object(m,'screenshot',return_value=shot),patch.object(m,'vision',side_effect=[state,{**state,'success':False}]):
            result,_,_=m.inspect('read page only',Path(d),'state',consensus=True,page_only=True)
            self.assertFalse(result['success']);self.assertFalse(result['target_found'])
unittest.main()
