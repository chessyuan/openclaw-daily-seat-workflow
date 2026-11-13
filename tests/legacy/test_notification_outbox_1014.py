import importlib.util,tempfile,unittest,time,json
from pathlib import Path

spec=importlib.util.spec_from_file_location('outbox',Path(__file__).with_name('android-notification-outbox.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        m.ROOT=Path(self.temp.name)/'queue';m.CONTEXT=Path(self.temp.name)/'context'
        m.CONTEXT.mkdir();self.context=m.CONTEXT/'bot.context-tokens.json'
        self.context.write_text('{"test":"test-context-old"}')
    def queue(self,text='result'):
        return m.track({'sent':False,'reason':'wechat_upstream_prepare_rejected'},text,'weixin','bot','recipient')
    def records(self):return list(m.ROOT.glob('*.json'))
    def fresh(self):self.context.write_text('{"test":"test-context-fresh"}')
    def test_queue_private_without_credentials(self):
        self.assertTrue(self.queue()['queued']);path=self.records()[0]
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertNotIn('test-context-old',path.read_text())
    def test_no_replay_same_context(self):
        self.queue();self.assertEqual(m.flush(lambda *a:self.fail('unexpected send'))['waiting'],1)
    def test_new_context_sends_once(self):
        self.queue();self.fresh();calls=[]
        self.assertEqual(m.flush(lambda *a:calls.append(a) or {'sent':True})['sent'],1)
        m.flush(lambda *a:self.fail('duplicate'));self.assertEqual(len(calls),1)
    def test_unknown_never_queued(self):
        m.track({'sent':False,'reason':'delivery_timeout_unknown_do_not_replay'},'result','weixin','bot','recipient')
        self.assertEqual(self.records(),[])
    def test_newer_replaces_old(self):
        self.queue('old warning');self.queue('new final')
        self.assertEqual(len(self.records()),1)
        self.assertEqual(json.loads(self.records()[0].read_text())['message'],'new final')
    def test_success_clears_stale_queue(self):
        self.queue();m.track({'sent':True},'new final','weixin','bot','recipient')
        self.assertEqual(self.records(),[])
    def test_fresh_rejection_waits_again(self):
        self.queue();self.fresh()
        self.assertEqual(m.flush(lambda *a:{'sent':False,'reason':'wechat_upstream_prepare_rejected'})['rejected'],1)
        m.flush(lambda *a:self.fail('tight retry'))
    def test_unknown_retired(self):
        self.queue();self.fresh();m.flush(lambda *a:{'sent':False,'reason':'delivery_timeout_unknown_do_not_replay'})
        self.assertEqual(self.records(),[])
    def test_expired_not_delivered(self):
        self.queue();path=self.records()[0];r=json.loads(path.read_text());r['createdAt']=time.time()-86401
        path.write_text(json.dumps(r))
        self.assertEqual(m.flush(lambda *a:self.fail('expired send'))['expired'],1)

if __name__=='__main__':unittest.main()
