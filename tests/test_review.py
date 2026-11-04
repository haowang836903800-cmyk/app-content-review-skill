import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('review', Path(__file__).parents[1] / 'scripts/feishu_review.py')
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)


def snap(xml='<p id="b">每天查一次<del>实时</del></p><source id="v" token="t" name="v.mp4"/><img id="i" src="pic" name="p.png"/>'):
    s = r.inspect_document({'document': {'document_id': 'd', 'revision_id': 1, 'content': xml}})
    s.update(url='https://example.invalid/docx/d', comments_complete=True, comments=[])
    return s


def req():
    return {'project': 'doubao-app-peiyao', 'audience': 'isolated-test', 'campaign': '测试',
            'bf': '测试BF', 'target_version': 'v1', 'review_scope': ['text'], 'decision': 'revise',
            'coverage': {x: True for x in ['bf_matched', 'body_read', 'comments_read', 'required_links_checked']},
            'issues': [{'block_id': 'b', 'kind': 'text', 'category': '必须修改', 'quote': '每天查一次',
                        'problem': '测试问题', 'change': '测试改法', 'acceptance': '测试验收',
                        'evidence': '当前原句', 'rule_source': '测试BF'}]}


def card(issue):
    return {'comment_id': 'c', 'is_whole': False, 'relation': {'block_ids': [issue['block_id']]},
            'reply_list': {'replies': [{'content': {'elements': [{'text_run': {'text': issue['text']}}]}}]}}


class ReviewTest(unittest.TestCase):
    def test_zero_issue_pass_is_local_and_writes_nothing(self):
        q = req(); q.update(decision='pass', issues=[])
        p = r.prepare(snap(), q)
        with tempfile.TemporaryDirectory() as d, patch.object(r, 'cli') as api, patch.object(r, 'comments') as c:
            out = r.apply_plan(p, Path(d)/'receipt.json')
            self.assertEqual(out['status'], 'no_comments_needed'); api.assert_not_called(); c.assert_not_called()

    def test_no_forced_revise_when_no_issue(self):
        q=req();q['issues']=[]
        with self.assertRaises(ValueError): r.prepare(snap(),q)

    def test_pass_cannot_hide_blocking_gap(self):
        q=req();q.update(decision='pass',issues=[],blocking_gaps=['未取得成片'])
        with self.assertRaises(ValueError):r.prepare(snap(),q)

    def test_scope_and_project_are_checked(self):
        q=req();q['project']='doubao-phone'
        with self.assertRaises(ValueError):r.prepare(snap(),q)
        q=req();q['review_scope']=['video']
        with self.assertRaises(ValueError):r.prepare(snap(),q)

    def test_deleted_quote_is_not_current(self):
        q=req();q['issues'][0]['quote']='实时'
        with self.assertRaises(ValueError):r.prepare(snap(),q)

    def test_comment_wrappers_do_not_invalidate_real_content(self):
        a=snap('<p id="b">甲<b>乙</b>丙</p>')
        b=snap('<p id="b" comment-refs="c"><span comment-refs="c">甲<b>乙</b></span>丙</p>')
        self.assertEqual(a['content_hash'],b['content_hash'])
        self.assertEqual(a['blocks']['b']['hash'],b['blocks']['b']['hash'])

    def test_actual_style_or_text_change_changes_hash(self):
        a=snap('<p id="b">甲<b>乙</b>丙</p>')
        for xml in ['<p id="b">甲乙丙</p>','<p id="b">甲<b>丁</b>丙</p>']:
            self.assertNotEqual(a['content_hash'],snap(xml)['content_hash'])

    def test_pagination_fails_if_cursor_missing_or_repeated(self):
        for values in [[{'items':[], 'has_more':True}],
                       [{'items':[], 'has_more':True,'page_token':'x'},{'items':[],'has_more':True,'page_token':'x'}]]:
            with patch.object(r,'cli',side_effect=values), self.assertRaises(RuntimeError):
                r.paginate('+list-comments')

    def test_replies_are_fully_fetched(self):
        pages=[{'items':[{'comment_id':'c','has_more':True}],'has_more':False},
               {'items':[{'reply_id':'1'}],'has_more':True,'page_token':'p'},
               {'items':[{'reply_id':'2'}],'has_more':False}]
        with patch.object(r,'cli',side_effect=pages):
            c=r.comments('url');self.assertEqual(len(c[0]['reply_list']['replies']),2)

    def test_real_change_stops_before_write(self):
        p=r.prepare(snap(),req())
        with tempfile.TemporaryDirectory() as d, patch.object(r,'comments',return_value=[]), patch.object(r,'fetch',return_value=snap('<p id="b">改过了</p>')), patch.object(r,'cli') as api:
            with self.assertRaises(RuntimeError):r.apply_plan(p,Path(d)/'out.json')
            api.assert_not_called()

    def test_success_requires_correct_anchor_and_repeat_is_noop(self):
        s=snap();p=r.prepare(s,req());c=card(p['issues'][0])
        with tempfile.TemporaryDirectory() as d, patch.object(r,'comments',side_effect=[[],[c],[c]]), patch.object(r,'fetch',return_value=s),patch.object(r,'cli',return_value={'id':'c'}) as api:
            self.assertEqual(r.apply_plan(p,Path(d)/'a.json')['results'][0]['status'],'verified')
            self.assertEqual(r.apply_plan(p,Path(d)/'b.json')['results'][0]['status'],'already_exists')
            self.assertEqual(api.call_count,1)

    def test_timeout_needs_reconciliation_not_blind_retry(self):
        p=r.prepare(snap(),req())
        with tempfile.TemporaryDirectory() as d, patch.object(r,'comments',return_value=[]),patch.object(r,'fetch',return_value=snap()),patch.object(r,'cli',side_effect=TimeoutError) as api:
            f=Path(d)/'a.json'
            with self.assertRaises(TimeoutError):r.apply_plan(p,f)
            self.assertEqual(api.call_count,1)
            self.assertEqual(json.loads(f.read_text())['results'][0]['status'],'needs_reconciliation')

    def test_whole_document_anchor_is_not_success(self):
        p=r.prepare(snap(),req());c=card(p['issues'][0]);c['is_whole']=True
        with tempfile.TemporaryDirectory() as d,patch.object(r,'comments',side_effect=[[],[c]]),patch.object(r,'fetch',return_value=snap()),patch.object(r,'cli',return_value={}):
            with self.assertRaises(RuntimeError):r.apply_plan(p,Path(d)/'a.json')

    def test_image_bound_to_current_bytes_and_block(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'p.png';f.write_bytes(b'fixture')
            q=req();q['review_scope']=['image'];i=q['issues'][0]
            i.update(block_id='i',kind='image',media={'token':'pic','path':str(f),'sha256':r.file_digest(f),'filename':'p.png','version':'v1','review_method':'原图'})
            i.pop('quote');r.prepare(snap(),q)
            f.write_bytes(b'changed')
            with self.assertRaises(ValueError):r.prepare(snap(),q)

    def test_video_time_source_and_audio_guard(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'v.mp4';f.write_bytes(b'fixture')
            q=req();q['review_scope']=['video'];i=q['issues'][0];i.pop('quote')
            i.update(block_id='v',kind='video',start_seconds=1,end_seconds=3,media={'token':'t','path':str(f),'sha256':r.file_digest(f),'filename':'v.mp4','version':'v1','duration_seconds':10,'review_method':'抽帧','audio_listened':False})
            r.prepare(snap(),q)
            i['requires_audio']=True
            with self.assertRaises(ValueError):r.prepare(snap(),q)
            i['requires_audio']=False;i['end_seconds']=11
            with self.assertRaises(ValueError):r.prepare(snap(),q)
            i['end_seconds']=float('nan')
            with self.assertRaises(ValueError):r.prepare(snap(),q)
            i['end_seconds']=3;i['media']['token']='other'
            with self.assertRaises(ValueError):r.prepare(snap(),q)

    def test_frame_asr_only_is_not_full_video_pass(self):
        q=req();q.update(decision='pass',issues=[],review_scope=['video'])
        q['coverage']['videos']=[{'continuous_viewed':False,'audio_listened':False}]
        with self.assertRaises(ValueError):r.prepare(snap(),q)


if __name__=='__main__': unittest.main()
