import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import icity_export as m
m.ensure_runtime_dependencies()


def payload(tokens, active, archived=0):
    return {'models':[{'i':'1001.owner','d':{'token':'owner','posts_count':active,'archived_posts_count':archived}}]+
            [{'i':'3001.'+token,'d':{'token':token,'comment_text':'正文-'+token,'created_at':1700000000,'archived':token=='archived'}} for token in tokens],
            'cmds':[[2611,'has_more',False]]}


class FakeClient:
    failure=None
    active_count=1
    active_tokens=['one']
    archive_tokens=[]
    def __init__(self): self.auth_token='never-publish-this-session'
    def sign_in(self, username, password):
        return {'auth_token':self.auth_token,'user':{'token':'owner','username':'testuser'}}
    def iter_pages(self,kind,user_token,max_pages=None):
        tokens=self.active_tokens if kind=='posts' else self.archive_tokens
        yield 1,payload(tokens,self.active_count,len(self.archive_tokens))
        if self.failure is not None: raise self.failure


class CliHarness(unittest.TestCase):
    def run_cli(self, directory, client=FakeClient, extra=()):
        argv=['icity_export.py','--username','testuser','--password','synthetic-password',
              '--no-interactive','--output-dir',directory,'--prefix','backup',*extra]
        output=io.StringIO()
        with patch('sys.argv',argv),patch('icity_native.NativeClient',client),patch.object(m,'login',return_value=None),patch.object(m,'fetch_web_post_count',return_value=None,create=True),patch.object(m.requests,'Session'),contextlib.redirect_stdout(output),contextlib.redirect_stderr(output):
            result=m.main()
        return result,output.getvalue()

class CliTests(CliHarness):
    def test_native_success_is_verified_and_credentials_are_not_saved(self):
        with tempfile.TemporaryDirectory() as d:
            result,log=self.run_cli(d)
            self.assertEqual(result,0,log)
            report=json.loads((Path(d)/'backup.report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'],'complete')
            self.assertEqual(report['exported_count'],1)
            self.assertEqual(len(json.loads((Path(d)/'backup.json').read_text(encoding='utf-8'))),1)
            for f in Path(d).rglob('*'):
                if f.is_file():
                    text=f.read_text(encoding='utf-8')
                    self.assertNotIn('synthetic-password',text)
                    self.assertNotIn('never-publish-this-session',text)

    def test_count_mismatch_keeps_partial_files_and_nonzero_exit(self):
        class Mismatched(FakeClient):active_count=2
        with tempfile.TemporaryDirectory() as d:
            result,log=self.run_cli(d,Mismatched)
            self.assertEqual(result,3,log)
            self.assertEqual(json.loads((Path(d)/'backup.report.json').read_text(encoding='utf-8'))['status'],'partial')
            self.assertEqual(len(json.loads((Path(d)/'backup.json').read_text(encoding='utf-8'))),1)
            self.assertNotIn('\n导出完成\n',log)

    def test_interruption_keeps_a_checkpoint(self):
        class Interrupted(FakeClient):failure=KeyboardInterrupt()
        with tempfile.TemporaryDirectory() as d:
            result,log=self.run_cli(d,Interrupted)
            self.assertEqual(result,130,log)
            self.assertEqual(len(json.loads((Path(d)/'backup.json').read_text(encoding='utf-8'))),1)
            self.assertEqual(json.loads((Path(d)/'backup.report.json').read_text(encoding='utf-8'))['status'],'partial')

    def test_network_failure_after_first_page_keeps_partial_files(self):
        class Failed(FakeClient):failure=RuntimeError('synthetic network failure')
        with tempfile.TemporaryDirectory() as d:
            result,log=self.run_cli(d,Failed)
            self.assertEqual(result,3,log)
            self.assertTrue((Path(d)/'backup.json').exists())

    def test_a_verified_empty_account_is_a_valid_export(self):
        class Empty(FakeClient):active_count=0;active_tokens=[]
        with tempfile.TemporaryDirectory() as d:
            result,log=self.run_cli(d,Empty)
            self.assertEqual(result,0,log)
            self.assertEqual(json.loads((Path(d)/'backup.json').read_text(encoding='utf-8')),[])

    def test_filename_prefix_cannot_escape_output_directory(self):
        with tempfile.TemporaryDirectory() as d:
            result,log=self.run_cli(d,extra=('--prefix','../outside'))
            self.assertEqual(result,2,log)

class FinalizationTests(CliHarness):
    def test_markdown_interruption_cannot_leave_complete_report(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(m,'write_split_markdown',side_effect=KeyboardInterrupt()):
                try:
                    result,log=self.run_cli(d)
                except KeyboardInterrupt:
                    self.fail('Finalization interruption escaped without updating the report')
            self.assertEqual(result,130,log)
            report=json.loads((Path(d)/'backup.report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'],'partial')

    def test_malformed_second_page_keeps_a_clear_partial_report(self):
        class Malformed(FakeClient):
            def iter_pages(client,kind,user_token,max_pages=None):
                yield 1,payload(['one'],2)
                yield 2,{'models':[None],'cmds':[[2611,'has_more',False]]}
        with tempfile.TemporaryDirectory() as d:
            result,log=self.run_cli(d,Malformed)
            self.assertEqual(result,3,log)
            report=json.loads((Path(d)/'backup.report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'],'partial')
            self.assertEqual(report['exported_count'],1)

    def test_overwrite_does_not_mix_stale_raw_pages(self):
        with tempfile.TemporaryDirectory() as d:
            raw=Path(d)/'backup_raw_native';raw.mkdir()
            (raw/'posts_0002.json').write_text('{"old":true}',encoding='utf-8')
            result,log=self.run_cli(d,extra=('--overwrite',))
            self.assertEqual(result,0,log)
            self.assertFalse((raw/'posts_0002.json').exists())
            self.assertTrue(list(Path(d).glob('backup_raw_native.previous-*')))

class AdditionalFailureTests(CliHarness):
    def test_invalid_photo_field_is_partial_not_a_crash(self):
        class MalformedPhotos(FakeClient):
            def iter_pages(client,kind,user_token,max_pages=None):
                yield 1,payload(['one'],2)
                bad=payload(['two'],2);bad['models'][1]['d']['photos']=7
                yield 2,bad
        with tempfile.TemporaryDirectory() as d:
            result,log=self.run_cli(d,MalformedPhotos)
            self.assertEqual(result,3,log)
            report=json.loads((Path(d)/'backup.report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'],'partial')
            self.assertEqual(report['exported_count'],1)

    def test_raw_write_failure_updates_the_still_writable_report(self):
        original=m.atomic_write_text
        def write(path,text):
            if '_raw_native' in str(path):raise OSError('synthetic raw disk failure')
            return original(path,text)
        with tempfile.TemporaryDirectory() as d:
            with patch.object(m,'atomic_write_text',side_effect=write):
                result,log=self.run_cli(d)
            self.assertEqual(result,1,log)
            self.assertEqual(json.loads((Path(d)/'backup.report.json').read_text(encoding='utf-8'))['status'],'failed')
