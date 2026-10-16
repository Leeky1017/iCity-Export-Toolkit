import hashlib
import json
import unittest
import zlib
from unittest.mock import Mock
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import icity_native as n


class ProtocolTests(unittest.TestCase):
    def test_request_payload_is_encrypted_without_losing_unicode(self):
        timestamp = 1700000000123
        payload = {'user': {'login': 'test-user', 'password': 'synthetic-only'}, 'text': '正文\n原样'}
        key = hashlib.sha256(b'icty').digest()[:16] + hashlib.sha256(str(timestamp).encode()).digest()[16:]
        encoded = n.encode_request(payload, timestamp)
        from Crypto.Util.Padding import unpad
        decoded = json.loads(zlib.decompress(unpad(AES.new(key, AES.MODE_CBC, bytes(16)).decrypt(encoded), 16)))
        self.assertEqual(decoded, payload)

    def test_response_uses_server_time_salt(self):
        timestamp = 1700000000
        salt = str(((timestamp & 0xFFFFFFFF) * 0xB5EB096B) >> 42)
        key = hashlib.sha256(b'icty').digest()[:16] + hashlib.sha256(salt.encode()).digest()[16:]
        payload = {'models': [], 'cmds': [[2611, 'has_more', False]], 'test': '中文'}
        ciphertext = AES.new(key, AES.MODE_CBC, bytes(16)).encrypt(pad(zlib.compress(json.dumps(payload).encode()), 16))
        self.assertEqual(n.decode_response(ciphertext, timestamp), payload)

    def test_truncated_ciphertext_is_rejected(self):
        with self.assertRaises(ValueError):
            n.decode_response(b'not-a-complete-block', 1700000000)

    def test_authentication_error_does_not_leak_response_or_token(self):
        session = Mock()
        response = Mock(status_code=401, headers={'Content-Type': 'application/json'})
        response.json.return_value = {'error': 'synthetic-secret-token'}
        session.get.return_value = response
        client = n.NativeClient(session=session)
        with self.assertRaisesRegex(RuntimeError, '401') as error:
            client.get_page('posts', 'testusertoken', 1)
        self.assertNotIn('synthetic-secret', str(error.exception))

    def test_successful_login_requires_explicit_auth_token(self):
        session = Mock()
        response = Mock(status_code=200, headers={'Content-Type': 'application/json'})
        response.json.return_value = {'token': 'profile-token-is-not-an-auth-token'}
        session.post.return_value = response
        with self.assertRaisesRegex(RuntimeError, '登录'):
            n.NativeClient(session=session).sign_in('test-user', 'synthetic-only')

    def test_read_client_only_allows_known_diary_collections(self):
        client = n.NativeClient(session=Mock())
        with self.assertRaises(ValueError):
            client.get_page('../../delete', 'testusertoken', 1)



class DefinitionNegotiationTests(unittest.TestCase):
    def test_server_definition_update_is_acknowledged_before_reading(self):
        session = Mock()
        negotiation = Mock(status_code=401, headers={'Content-Type': 'application/json'})
        negotiation.json.return_value = {'definitions': {'version': '2099-01-01 00:00:00 +0800'}}
        response = Mock(status_code=200, headers={'Content-Type': 'application/json'})
        response.json.return_value = {'models': [], 'cmds': [[2611, 'has_more', False]]}
        session.get.side_effect = [negotiation, response]
        client = n.NativeClient(session=session)
        result = client.get_page('posts', 'testuser', 1)
        self.assertEqual(result['models'], [])
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.get.call_args.kwargs['headers']['IC-Def-Ver'], '2099-01-01 00:00:00 +0800')

class NativePaginationTests(unittest.TestCase):
    def payload(self, tokens, has_more):
        return {'models':[{'i':'3001.'+t,'d':{'token':t}} for t in tokens],
                'cmds':[[2611,'has_more',has_more]]}

    def test_iteration_stops_on_server_has_more_false(self):
        client=n.NativeClient(session=Mock())
        client.get_page=Mock(side_effect=[self.payload(['a'],True),self.payload(['b'],False)])
        with unittest.mock.patch.object(n.time,'sleep'):
            pages=list(client.iter_pages('posts','testuser'))
        self.assertEqual(len(pages),2)
        self.assertEqual(client.get_page.call_count,2)

    def test_native_repetition_is_not_silently_complete(self):
        client=n.NativeClient(session=Mock())
        client.get_page=Mock(side_effect=[self.payload(['a'],True),self.payload(['a'],True)])
        with unittest.mock.patch.object(n.time,'sleep'),self.assertRaisesRegex(RuntimeError,'重复'):
            list(client.iter_pages('posts','testuser',max_pages=3))

    def test_missing_pagination_metadata_is_not_end_of_account(self):
        client=n.NativeClient(session=Mock())
        client.get_page=Mock(return_value={'models':[],'cmds':[]})
        with self.assertRaisesRegex(RuntimeError,'分页'):
            list(client.iter_pages('posts','testuser'))

    def test_native_page_cap_is_not_complete(self):
        client=n.NativeClient(session=Mock())
        client.get_page=Mock(return_value=self.payload(['a'],True))
        with self.assertRaisesRegex(RuntimeError,'上限'):
            list(client.iter_pages('posts','testuser',max_pages=1))

if __name__ == '__main__':
    unittest.main()
