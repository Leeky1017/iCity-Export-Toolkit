"""Read-only iCity native API transport.

Protocol details adapted from chrisprown902-wq/icity-exporter (MIT).
See THIRD_PARTY_NOTICES.md. This module does not save credentials or sessions.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
import zlib

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_BASE = 'https://icity.ly/api/v2'
DEFINITION_VERSION = '2026-08-26 16:16:00 +0800'


def _key(second: str) -> bytes:
    return hashlib.sha256(b'icty').digest()[:16] + hashlib.sha256(second.encode('utf-8')).digest()[16:]


def encode_request(payload: dict, milliseconds: int) -> bytes:
    plain = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    return AES.new(_key(str(milliseconds)), AES.MODE_CBC, bytes(16)).encrypt(pad(zlib.compress(plain), 16))


def decode_response(ciphertext: bytes, server_time: int) -> dict:
    if not ciphertext or len(ciphertext) % 16:
        raise ValueError('服务器响应不完整，拒绝截断后继续解析')
    salt = str(((server_time & 0xFFFFFFFF) * 0xB5EB096B) >> 42)
    plain = unpad(AES.new(_key(salt), AES.MODE_CBC, bytes(16)).decrypt(ciphertext), 16)
    data = json.loads(zlib.decompress(plain))
    if not isinstance(data, dict):
        raise ValueError('服务器响应不是预期的数据对象')
    return data


class NativeClient:
    def __init__(self, session=None):
        self.session = session if session is not None else requests.Session()
        if session is None:
            self.session.mount('https://', HTTPAdapter(max_retries=Retry(
                total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=frozenset({'GET'}), respect_retry_after_header=True,
            )))
        self.auth_token = ''
        self.device_uuid = str(uuid.uuid4())
        self.definition_version = DEFINITION_VERSION

    def _headers(self):
        headers = {
            'User-Agent': 'iCity/1.12 (com.chii.icity; build:656)',
            'Accept': 'application/json',
            'IC-Device': 'desktop-iCityExport-1',
            'IC-Locale': 'zh-Hans-CN',
            'IC-App': 'ICTY-1.12',
            'IC-Auth-App': 'ICTY-1.12',
            'IC-Cloud-V': '2',
            'IC-Def-Ver': self.definition_version,
        }
        if self.auth_token:
            headers['IC-Auth-Token'] = self.auth_token
        return headers

    def _decode(self, response):
        if 'clhaze' in response.headers.get('Content-Type', ''):
            server_time = response.headers.get('ICTY-Time') or response.headers.get('CL-SYNC-Time')
            if server_time is None:
                raise RuntimeError('原生接口缺少响应时间，无法验证内容')
            try:
                return decode_response(response.content, int(server_time))
            except (ValueError, zlib.error) as exc:
                raise RuntimeError('原生接口内容无法解析；可能是协议已更新') from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError('原生接口未返回可识别的数据') from exc
        if not isinstance(data, dict):
            raise RuntimeError('原生接口数据结构异常')
        return data

    def _read(self, response):
        # Never print response bodies or headers: they can contain session secrets.
        if response.status_code != 200:
            raise RuntimeError(f'iCity 原生接口返回 HTTP {response.status_code}；未将错误响应当成日记')
        return self._decode(response)

    def _definition_changed(self, response):
        if response.status_code not in (200, 401):
            return False
        try:
            data = self._decode(response)
        except RuntimeError:
            return False
        definitions = data.get('definitions')
        version = definitions.get('version') if isinstance(definitions, dict) else None
        if isinstance(version, str) and version and version != self.definition_version:
            self.definition_version = version
            return True
        return False

    def sign_in(self, username: str, password: str) -> dict:
        milliseconds = int(time.time() * 1000)
        payload = {
            'user': {'login': username, 'password': password},
            'device': {'device_uuid': self.device_uuid, 'device_name': 'iCity Export Toolkit',
                       'device_model': 'Python', 'client_info': {'pro': False}},
        }
        headers = self._headers()
        headers.update({'Content-Type': 'application/json', 'IC-Post-At': str(milliseconds),
                        'IC-Post-Z': '1', 'Connection': 'close'})
        encoded = encode_request(payload, milliseconds)
        for attempt in range(2):
            headers['IC-Def-Ver'] = self.definition_version
            response = self.session.post(API_BASE + '/users/sign_in', data=encoded,
                                         headers=headers.copy(), timeout=30, allow_redirects=False)
            if attempt == 0 and self._definition_changed(response):
                continue
            break
        data = self._read(response)
        token = data.get('auth_token')
        if not isinstance(token, str) or not token:
            raise RuntimeError('原生接口登录未成功：未取得登录凭据，不能用个人ID代替')
        self.auth_token = token
        return data

    def get_page(self, kind: str, user_token: str, page: int) -> dict:
        if kind not in {'posts', 'archived_posts', 'photos'}:
            raise ValueError('仅支持读取日记、封存日记和图片索引')
        if not isinstance(user_token, str) or not user_token.isalnum() or page < 1:
            raise ValueError('用户标识或页码无效')
        for attempt in range(2):
            response = self.session.get(API_BASE + f'/icty/users/{user_token}/{kind}',
                                        params={'page': page}, headers=self._headers(),
                                        timeout=30, allow_redirects=False)
            if attempt == 0 and self._definition_changed(response):
                continue
            break
        return self._read(response)

    def iter_pages(self, kind: str, user_token: str, max_pages=None):
        """Yield checked pages; only an explicit server end marker finishes the stream."""
        seen = set()
        page = 1
        model_type = '3003.' if kind == 'photos' else '3001.'
        while True:
            data = self.get_page(kind, user_token, page)
            models = data.get('models')
            if not isinstance(models, list):
                raise RuntimeError('原生接口缺少日记列表；本次下载未完成')
            if any(not isinstance(m, dict) or not isinstance(m.get('i'), str) or not isinstance(m.get('d'), dict) for m in models):
                raise RuntimeError('原生接口数据模型格式异常；本次下载未完成')
            commands = data.get('cmds')
            if not isinstance(commands, list):
                raise RuntimeError('原生接口分页命令格式异常')
            flags = [c[2] for c in commands
                     if isinstance(c, list) and len(c) >= 3 and c[0] == 2611 and c[1] == 'has_more']
            if not flags or not isinstance(flags[-1], bool):
                raise RuntimeError('原生接口缺少有效分页标记；不能判断是否完整')
            has_more = flags[-1]
            ids = {m.get('i') for m in models if isinstance(m, dict) and str(m.get('i', '')).startswith(model_type)}
            if ids and not ids.difference(seen):
                raise RuntimeError('原生接口返回重复日记页；已停止以避免无限循环')
            if not ids and has_more:
                raise RuntimeError('原生接口返回空页但仍声称有更多；本次下载未完成')
            seen.update(ids)
            yield page, data
            if not has_more:
                return
            if max_pages is not None and page >= max_pages:
                raise RuntimeError('已达到页数上限；仅导出了部分日记')
            page += 1
            time.sleep(0.4)
