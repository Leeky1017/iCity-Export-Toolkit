import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import icity_export as m
m.ensure_runtime_dependencies()


def post(token='one', text='正文<br>第二行', dated=True):
    date = ' title="2026-01-02 03:04" datetime="2026-01-01T19:04:00Z"' if dated else ''
    return f'<li class="diary"><div class="line"><div class="comment">{text}</div></div><div class="meta"><a class="timeago" href="/a/{token}"><time class="hours"{date}>03:04</time></a></div></li>'


def page(*tokens, next_url=None, hint=False):
    nav = f'<li><a rel="next" data-role="load-more-trigger" href="{next_url}">next</a></li>' if next_url else ''
    if hint:
        nav += '<li><div class="load-more-download-hint">下载 iCity 查看更多</div></li>'
    return '<ul class="posts-list">'+''.join(post(t) for t in tokens)+'</ul><ul class="ic-pagination" data-role="pagination">'+nav+'</ul>'


class FakeResponse:
    def __init__(self, html): self.text = html
    def raise_for_status(self): pass


class FakeSession:
    def __init__(self, pages): self.pages=list(pages); self.urls=[]
    def get(self, url, **kwargs):
        self.urls.append(url)
        value=self.pages.pop(0) if self.pages else page()
        if isinstance(value, BaseException): raise value
        return FakeResponse(value)


class WebPaginationTests(unittest.TestCase):
    def setUp(self):
        self.sleep=patch.object(m.time,'sleep');self.sleep.start();self.addCleanup(self.sleep.stop)
        self.stdout=io.StringIO();self.quiet=contextlib.redirect_stdout(self.stdout);self.quiet.__enter__();self.addCleanup(self.quiet.__exit__,None,None,None)
        self.url=m.build_posts_url('testuser')

    def test_follows_the_actual_continuation_url(self):
        session=FakeSession([page('one',next_url='/u/testuser/posts?cursor=older'),page('two')])
        entries=m.scrape_all(session,self.url)
        self.assertEqual([e.id for e in entries],['one','two'])
        self.assertEqual(session.urls,[self.url,self.url+'?cursor=older'])

    def test_terminal_full_page_is_not_requested_again(self):
        session=FakeSession([page('one','two')])
        self.assertEqual(len(m.scrape_all(session,self.url)),2)
        self.assertEqual(len(session.urls),1)

    def test_app_history_hint_is_explicitly_partial(self):
        session=FakeSession([page('one',hint=True)])
        with self.assertRaisesRegex(RuntimeError,'全量|部分|网页'):
            m.scrape_all(session,self.url)

    def test_repeated_page_does_not_keep_incrementing(self):
        repeat=page('one',next_url='/u/testuser/posts?page=2')
        session=FakeSession([repeat,repeat,page()])
        with self.assertRaisesRegex(RuntimeError,'重复|循环'):
            m.scrape_all(session,self.url,max_pages=3)
        self.assertLessEqual(len(session.urls),2)

    def test_progress_counts_new_ids_instead_of_all_rows(self):
        session=FakeSession([page('one','two',next_url='/u/testuser/posts?page=2'),page('two','three')])
        self.assertEqual(len(m.scrape_all(session,self.url)),3)
        self.assertIn('累计 3 条',self.stdout.getvalue())

    def test_missing_diary_markup_is_not_an_empty_success(self):
        with self.assertRaisesRegex(RuntimeError,'结构|列表'):
            m.scrape_all(FakeSession(['<html>unexpected template</html>']),self.url)

    def test_explicit_page_cap_is_not_complete(self):
        with self.assertRaisesRegex(RuntimeError,'上限|max-pages|部分'):
            m.scrape_all(FakeSession([page('one',next_url='/u/testuser/posts?page=2')]),self.url,max_pages=1)

    def test_foreign_continuation_url_is_not_followed(self):
        session=FakeSession([page('one',next_url='https://example.invalid/collect')])
        with self.assertRaisesRegex(RuntimeError,'地址|站点|分页'):
            m.scrape_all(session,self.url)
        self.assertEqual(len(session.urls),1)


class ContentAndOutputTests(unittest.TestCase):
    def test_inline_markup_does_not_add_false_line_breaks(self):
        entries=m.extract_entries_from_page('<ul class="posts-list">'+post(text='今天<a href="https://example.invalid">链接</a>很好<br>下一行')+'</ul>')
        self.assertEqual(entries[0].text,'今天链接很好\n下一行')

    def test_unknown_dates_are_not_dropped_from_markdown(self):
        entries=m.extract_entries_from_page('<ul class="posts-list">'+post(dated=False)+'</ul>')
        with tempfile.TemporaryDirectory() as d:
            count=m.write_split_markdown(entries,d+'/md')
            files=list(Path(d+'/md').rglob('*.md'))
            self.assertEqual(count,1)
            self.assertIn('ID: `one`','\n'.join(p.read_text(encoding='utf-8') for p in files))

    def test_json_and_text_keep_the_same_records(self):
        entries=m.extract_entries_from_page(page('one','two'))
        with tempfile.TemporaryDirectory() as d:
            j,t=m.build_output_paths(d,'test')
            m.write_outputs(entries,j,t)
            self.assertEqual(len(json.loads(Path(j).read_text(encoding='utf-8'))),2)
            self.assertEqual(Path(t).read_text(encoding='utf-8').count('ID:'),2)



class NativeContentTests(unittest.TestCase):
    def payload(self):
        return {'models':[
            {'i':'1001.owner','d':{'token':'owner','posts_count':1,'archived_posts_count':0}},
            {'i':'3001.one','d':{'token':'one','created_at':1700000000,'title':'标题','comment_text':' 正文\n第二行 ','comment':[{'t':'正文'}],'user':'1001.owner','photos':['3003.photo'],'privacy':3,'archived':False}},
            {'i':'3003.photo','d':{'token':'photo','url':'https://example.invalid/photo.jpg'}}],
            'cmds':[[2611,'has_more',False]]}

    def test_native_plain_text_and_original_metadata_are_preserved(self):
        entries=m.extract_native_entries(self.payload())
        self.assertEqual(len(entries),1)
        self.assertEqual(entries[0].text,' 正文\n第二行 ')
        self.assertEqual(entries[0].privacy,3)
        self.assertEqual(entries[0].photos[0]['url'],'https://example.invalid/photo.jpg')
        self.assertEqual(entries[0].native_data['user'],'1001.owner')

    def test_native_rich_text_fallback_keeps_inline_text_together(self):
        payload=self.payload();data=payload['models'][1]['d'];del data['comment_text']
        data['comment']=[{'t':'你好'},{'t':'链接','as':{'lk':'https://example.invalid'}},'\n下一行']
        self.assertEqual(m.extract_native_entries(payload)[0].text,'你好链接\n下一行')

    def test_checkpoint_survives_later_native_failure(self):
        class Client:
            def iter_pages(client,kind,user_token,max_pages=None):
                yield 1,self.payload()
                raise RuntimeError('synthetic network failure')
        calls=[]
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(m.PartialExportError) as error:
                m.scrape_native(Client(),'owner',{},on_progress=lambda rows,pages:calls.append([e.id for e in rows]),raw_dir=d)
            self.assertEqual(calls,[['one']])
            self.assertEqual(error.exception.entries[0].id,'one')
            self.assertEqual(len(list(Path(d).glob('*.json'))),1)

    def test_native_collection_count_mismatch_is_not_complete(self):
        class Client:
            def iter_pages(client,kind,user_token,max_pages=None):
                payload=self.payload()
                payload['models'][0]['d']['posts_count']=2
                yield 1,payload
        with self.assertRaisesRegex(RuntimeError,'数量|条数'):
            m.scrape_native(Client(),'owner',{},include_archived=False)

    def test_atomic_output_failure_does_not_destroy_previous_json(self):
        with tempfile.TemporaryDirectory() as d:
            j,t=m.build_output_paths(d,'test');Path(j).write_text('previous',encoding='utf-8')
            with patch.object(m.os,'replace',side_effect=OSError('synthetic disk failure')):
                with self.assertRaises(OSError):m.write_outputs([],j,t)
            self.assertEqual(Path(j).read_text(encoding='utf-8'),'previous')

class CountSourceTests(unittest.TestCase):
    def test_independently_read_web_profile_count_can_reconcile_native_counter(self):
        class Client:
            def iter_pages(self,kind,user_token,max_pages=None):
                yield 1,{'models':[{'i':'1001.owner','d':{'token':'owner','posts_count':1,'archived_posts_count':0}},
                                   {'i':'3001.one','d':{'token':'one','comment_text':'text'}},
                                   {'i':'3001.two','d':{'token':'two','comment_text':'text'}}],
                         'cmds':[[2611,'has_more',False]]}
        metadata={'web_posts_count':2}
        self.assertEqual(len(m.scrape_native(Client(),'owner',metadata,include_archived=False)),2)
        self.assertTrue(metadata.get('count_warnings'))

class ExportSafetyTests(unittest.TestCase):
    def test_unknown_body_blocks_are_not_silently_exported_as_blank(self):
        data={'models':[{'i':'3001.one','d':{'token':'one','comment':[{'text':'unexpected-schema'}]}}]}
        with self.assertRaisesRegex(RuntimeError,'正文|格式'):
            m.extract_native_entries(data)

    def test_higher_native_counter_cannot_be_overridden_by_lower_web_count(self):
        class Client:
            def iter_pages(self,kind,user_token,max_pages=None):
                yield 1,{'models':[{'i':'1001.owner','d':{'posts_count':2}},
                                  {'i':'3001.one','d':{'token':'one','comment_text':'text'}}],
                         'cmds':[[2611,'has_more',False]]}
        with self.assertRaisesRegex(RuntimeError,'数量|条数'):
            m.scrape_native(Client(),'owner',{'web_posts_count':1},include_archived=False)


class LoginSafetyTests(unittest.TestCase):
    def session(self,status,location=None,profile='<a href="/users/sign_out">logout</a>'):
        from unittest.mock import Mock
        session=Mock()
        welcome=Mock(text='<meta name="csrf-token" content="synthetic-csrf">')
        session.get.side_effect=[welcome,Mock(text=profile,status_code=200)]
        session.post.return_value=Mock(text='',status_code=status,headers={'Location':location} if location else {})
        return session

    def test_password_post_never_follows_redirects(self):
        session=self.session(307,'https://example.invalid/steal')
        with self.assertRaisesRegex(RuntimeError,'重定向|登录'):
            m.login(session,'testuser','synthetic-only','testuser')
        self.assertIs(session.post.call_args.kwargs['allow_redirects'],False)

    def test_public_profile_does_not_prove_successful_login(self):
        session=self.session(302,'/',profile='<h1>A public profile</h1>')
        with self.assertRaisesRegex(RuntimeError,'登录|登入'):
            m.login(session,'testuser','synthetic-only','testuser')

if __name__ == '__main__':
    unittest.main()
