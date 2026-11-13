import importlib.util,json,tempfile,unittest,subprocess
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
s=importlib.util.spec_from_file_location('m','/tmp/android-signin-1021-speed.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)

class Tests(unittest.TestCase):
    def setUp(self):
        m.BUSINESS_DEADLINE=None;m.RUN_CONTEXT.clear()
    def test_observe_uses_existing_gateway_fast_helper(self):
        with patch.object(m,'run_json',return_value={'ok':True,'payload':{'snapshotId':'s'}}) as rpc:
            self.assertEqual(m.observe()['snapshotId'],'s')
            argv=rpc.call_args.args[0]
            self.assertEqual(argv[2:4],['rpc','mobile.ui.observe'])
            self.assertNotIn(m.OPENCLAW,argv)
    def test_launch_keeps_native_timeout_budget(self):
        with patch.object(m,'run_json',return_value={'ok':True,'payload':{}}) as rpc:
            m.invoke('mobile.ui.launch',{'package':m.WECHAT},timeout=70)
            self.assertEqual(rpc.call_args.args[0][-1],'70000')
            self.assertEqual(rpc.call_args.kwargs['timeout'],70)
    def test_command_allowlist_cannot_expand(self):
        with patch.object(m,'run_json') as rpc:
            with self.assertRaises(m.FlowError):m.invoke('system.run',{})
            rpc.assert_not_called()
    def test_expired_window_launch_never_reaches_node(self):
        m.BUSINESS_DEADLINE=m.datetime.now(m.TZ)-timedelta(seconds=1)
        with patch.object(m,'run_json') as rpc:
            with self.assertRaises(m.WindowClosed):m.invoke('mobile.ui.launch',{'package':m.WECHAT})
            rpc.assert_not_called()
    def test_native_error_is_not_silently_success(self):
        with patch.object(m,'run_json',return_value={'ok':False,'payload':{}}):
            with self.assertRaises(m.FlowError):m.invoke('mobile.ui.launch',{'package':m.WECHAT})
    def test_act_stale_still_means_no_gesture(self):
        with patch.object(m,'run_json',return_value={'ok':True,'payload':{'code':'target_stale'}}):
            with self.assertRaises(m.ActionNotSubmitted):m.act({'snapshotId':'old'},{'type':'tap','x':1,'y':1})
    def test_status_online_capabilities_unchanged(self):
        node={'displayName':m.NODE,'connected':True,'commands':['mobile.ui.observe','mobile.ui.act','mobile.ui.launch','mobile.ui.screenshot']}
        with patch.object(m,'run_json',return_value={'ok':True,'nodes':[node]}) as rpc:
            m.ensure_node();self.assertEqual(rpc.call_args.args[0][-1],'status')
            self.assertTrue(m.RUN_CONTEXT['nodeConnection']['connected'])
    def test_offline_status_preserves_known_retry_category(self):
        node={'displayName':m.NODE,'connected':False,'lastSeenReason':'background'}
        with patch.object(m,'run_json',return_value={'ok':True,'nodes':[node]}):
            with self.assertRaisesRegex(m.FlowError,'Android Node 离线'):m.ensure_node()
            self.assertEqual(m.RUN_CONTEXT['nodeConnection']['lastSeenReason'],'background')
    def test_missing_capability_blocks_workflow(self):
        node={'displayName':m.NODE,'connected':True,'commands':['mobile.ui.observe']}
        with patch.object(m,'run_json',return_value={'ok':True,'nodes':[node]}):
            with self.assertRaisesRegex(m.FlowError,'缺少'):m.ensure_node()
    def test_snapshot_id_not_replaced_when_act_forwarded(self):
        with patch.object(m,'run_json',return_value={'ok':True,'payload':{'code':'completed'}}) as rpc:
            m.act({'snapshotId':'bound-original'},{'type':'global_action','name':'back'})
            payload=json.loads(rpc.call_args.args[0][4])
            self.assertEqual(payload['snapshotId'],'bound-original')
    def test_helper_offline_json_retains_retry_category(self):
        proc=subprocess.CompletedProcess([],1,json.dumps({'ok':False,'error':{'message':'Android Node 离线'}}),'')
        with patch.object(m.subprocess,'run',return_value=proc):
            with self.assertRaisesRegex(m.FlowError,'^Android Node 离线$'):m.fast_coordinate('global','home')
    def test_recents_reobserves_only_after_explicit_rejection(self):
        item=({'target_found':True,'confidence':.99,'evidence':'clear'},945,2048)
        with tempfile.TemporaryDirectory() as d,patch.object(m,'observe',return_value={'snapshotId':'s'}),patch.object(m,'act'),patch.object(m,'inspect',return_value=item) as inspect,patch.object(m,'tap_visual',side_effect=[m.ActionNotSubmitted('changed'),{}]) as tap,patch.object(m.time,'sleep'):
            result=m.clear_recent_apps(Path(d),1440,3120)
            self.assertTrue(result['cleared']);self.assertEqual(inspect.call_count,2);self.assertEqual(tap.call_count,2)
    def test_recents_unknown_timeout_is_not_replayed(self):
        item=({'target_found':True,'confidence':.99,'evidence':'clear'},945,2048)
        with tempfile.TemporaryDirectory() as d,patch.object(m,'observe',return_value={'snapshotId':'s'}),patch.object(m,'act'),patch.object(m,'inspect',return_value=item),patch.object(m,'tap_visual',side_effect=TimeoutError) as tap,patch.object(m.time,'sleep'):
            with self.assertRaises(TimeoutError):m.clear_recent_apps(Path(d),1440,3120)
            tap.assert_called_once()

if __name__=='__main__':unittest.main()
