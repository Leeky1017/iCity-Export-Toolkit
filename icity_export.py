#!/usr/bin/env python3
from __future__ import annotations
"""
iCity 日记导出脚本（非技术用户版）

你只需要准备 iCity 的登录账号和密码，脚本会自动：
1) 登录
2) 按服务器的“加载更多”信号读取普通和封存日记
3) 保存原始分页、JSON、TXT、按日 Markdown 和核对报告
"""

import argparse
import copy
import tempfile
from datetime import datetime, timezone, timedelta
import getpass
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode, quote

# 运行时依赖会在脚本内自动安装，这里先占位。
requests = None
BeautifulSoup = None

# 网站基础地址（一般不需要改）
BASE = "https://icity.ly"
LOGIN_PAGE = f"{BASE}/welcome"
LOGIN_POST = f"{BASE}/users/sign_in"
BOOTSTRAP_ENV_FLAG = "ICITY_EXPORT_BOOTSTRAPPED"
DEPENDENCY_PACKAGES = ["requests", "beautifulsoup4", "pycryptodome"]


@dataclass
class Entry:
    """每条日记的结构化数据。"""

    id: str
    date_label: str
    datetime_iso: str
    datetime_local: str
    time_label: str
    title: str
    text: str
    location: str
    source_url: str
    privacy: Optional[int] = None
    archived: bool = False
    photos: List[dict] = field(default_factory=list)
    native_data: Optional[dict] = None


class PartialExportError(RuntimeError):
    """The successfully read records remain available even when enumeration fails."""
    def __init__(self, message, entries, pages_fetched=0):
        super().__init__(message)
        self.entries = entries
        self.pages_fetched = pages_fetched


def build_posts_url(target_user: str) -> str:
    """根据用户名拼出日记列表地址。"""
    return f"{BASE}/u/{quote(target_user, safe='')}/posts"


def prompt_with_default(
    prompt: str,
    default_value: str,
    input_fn: Callable[[str], str] = input,
) -> str:
    """向用户提问；如果用户直接回车，则使用默认值。"""
    if default_value:
        value = input_fn(f"{prompt}（默认：{default_value}）：").strip()
        return value or default_value
    return input_fn(f"{prompt}：").strip()


def prompt_yes_no(
    prompt: str,
    default_yes: bool = True,
    input_fn: Callable[[str], str] = input,
) -> bool:
    """是/否提问，默认值可配置。"""
    default_text = "Y/n" if default_yes else "y/N"
    raw = input_fn(f"{prompt}（{default_text}）：").strip().lower()
    if raw == "":
        return default_yes
    return raw in {"y", "yes", "是", "1"}


def get_venv_python_path(venv_dir: str, os_name: Optional[str] = None) -> str:
    """返回虚拟环境里的 Python 路径，兼容 macOS/Linux/Windows。"""
    current_os = os_name or os.name
    if current_os == "nt":
        return str(PureWindowsPath(venv_dir) / "Scripts" / "python.exe")
    return os.path.join(venv_dir, "bin", "python3")


def bootstrap_dependencies_and_rerun() -> None:
    """
    自动安装依赖并重启脚本。
    首次运行会创建 .icity_export_venv（在脚本同目录）。
    """
    import venv

    script_path = os.path.abspath(__file__)
    script_dir = os.path.dirname(script_path)
    venv_dir = os.path.join(script_dir, ".icity_export_venv")
    venv_python = get_venv_python_path(venv_dir)

    print("检测到缺少依赖，正在自动准备运行环境（首次约 1-3 分钟）...", flush=True)

    if not os.path.exists(venv_python):
        venv.create(venv_dir, with_pip=True)

    install_cmd = [
        venv_python,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        *DEPENDENCY_PACKAGES,
    ]
    subprocess.check_call(install_cmd)

    rerun_cmd = [venv_python, script_path, *sys.argv[1:]]
    rerun_env = os.environ.copy()
    rerun_env[BOOTSTRAP_ENV_FLAG] = "1"
    subprocess.check_call(rerun_cmd, env=rerun_env)


def ensure_runtime_dependencies() -> None:
    """确保 requests / bs4 / Crypto 可用；不可用时自动安装后重跑。"""
    global requests, BeautifulSoup

    if requests is not None and BeautifulSoup is not None:
        return

    try:
        import requests as _requests
        from bs4 import BeautifulSoup as _BeautifulSoup
        from Crypto.Cipher import AES as _AES
    except ModuleNotFoundError as exc:
        missing = (exc.name or "").split(".")[0]
        if missing not in {"requests", "bs4", "Crypto"}:
            raise

        if os.environ.get(BOOTSTRAP_ENV_FLAG) == "1":
            raise RuntimeError(
                "自动安装依赖后仍无法导入 requests/bs4/Crypto，请检查网络或 Python 环境。"
            ) from exc

        bootstrap_dependencies_and_rerun()
        raise SystemExit(0) from exc

    requests = _requests
    BeautifulSoup = _BeautifulSoup


def clean_text(s: str) -> str:
    """清理网页里常见的空白字符。"""
    s = s.replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def html_to_text_with_breaks(node) -> str:
    """Keep explicit paragraph breaks without splitting inline links or emphasis."""
    node = copy.deepcopy(node)
    for br in node.find_all("br"):
        br.replace_with("\n")
    for block in node.find_all(["p", "div", "li"]):
        block.append("\n")
    return node.get_text("", strip=False).strip()


def get_csrf_token(html: str) -> Optional[str]:
    """从页面里提取登录需要的 CSRF token。"""
    soup = BeautifulSoup(html, "html.parser")
    meta_token = soup.find("meta", attrs={"name": "csrf-token"})
    if meta_token and meta_token.get("content"):
        return meta_token["content"]

    input_token = soup.find("input", attrs={"name": "authenticity_token"})
    if input_token and input_token.get("value"):
        return input_token["value"]

    return None


def resolve_credentials(
    username: Optional[str],
    password: Optional[str],
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = getpass.getpass,
) -> Tuple[str, str]:
    """
    统一处理账号密码来源：
    - 传了参数就用参数
    - 没传就交互式询问
    """
    final_username = (username or "").strip()
    if not final_username:
        final_username = input_fn("请输入 iCity 登录用户名或邮箱：").strip()

    if not final_username:
        raise ValueError("用户名不能为空")

    final_password = password
    if final_password is None:
        # 注意：这里输入时不会回显密码，安全性更好
        final_password = getpass_fn("请输入登录密码（输入时不会显示）：")

    if not final_password:
        raise ValueError("密码不能为空")

    return final_username, final_password


def build_output_paths(output_dir: str, prefix: str) -> Tuple[str, str]:
    """生成输出文件路径，并确保目录存在。"""
    final_output_dir = output_dir or "."
    os.makedirs(final_output_dir, exist_ok=True)

    json_path = os.path.join(final_output_dir, f"{prefix}.json")
    txt_path = os.path.join(final_output_dir, f"{prefix}.txt")
    return json_path, txt_path


def login(session: requests.Session, username: str, password: str, target_user: str) -> None:
    """登录 iCity，并验证会话是否可访问目标用户页面。"""
    login_page_resp = session.get(LOGIN_PAGE, timeout=30)
    login_page_resp.raise_for_status()

    token = get_csrf_token(login_page_resp.text)
    if not token:
        raise RuntimeError("无法获取登录 token，请稍后重试")

    payload = {
        "utf8": "✓",
        "authenticity_token": token,
        "icty_user[login]": username,
        "icty_user[password]": password,
        "icty_user[remember_me]": "1",
        "commit": "登入",
    }
    headers = {
        "Referer": LOGIN_PAGE,
        "Origin": BASE,
        "User-Agent": "Mozilla/5.0",
    }

    login_resp = session.post(
        LOGIN_POST,
        data=payload,
        headers=headers,
        timeout=30,
        allow_redirects=False,
    )
    login_resp.raise_for_status()
    if 300 <= login_resp.status_code < 400:
        location = urlsplit(urljoin(BASE, login_resp.headers.get("Location", "")))
        if login_resp.status_code in (307, 308) or (location.scheme, location.netloc) != ("https", "icity.ly"):
            raise RuntimeError("网页登录出现不安全的重定向，未转发账号密码")
    if is_login_page(login_resp.text):
        raise RuntimeError("登录失败：账号或密码不正确，或需要额外验证")

    # 再访问一次用户主页，确认确实已登录
    probe_resp = session.get(f"{BASE}/u/{quote(target_user, safe='')}", timeout=30, allow_redirects=False)
    probe_resp.raise_for_status()

    page_text = probe_resp.text
    if probe_resp.status_code != 200 or BeautifulSoup(page_text, "html.parser").select_one('a[href="/users/sign_out"]') is None:
        raise RuntimeError("网页登录未成功；公开个人主页不能作为登录成功的证据")
    if "开始使用网页版" in page_text and "用户名 / Email" in page_text:
        raise RuntimeError("登录失败：账号或密码不正确，或触发了额外验证")


def extract_entries_from_page(html: str) -> List[Entry]:
    """从单个分页里提取全部日记条目。"""
    soup = BeautifulSoup(html, "html.parser")
    posts_list = soup.select_one("ul.posts-list")
    if not posts_list:
        return []

    entries: List[Entry] = []
    current_date = ""

    for li in posts_list.find_all("li", recursive=False):
        classes = li.get("class", [])
        if "day-cut" in classes:
            current_date = clean_text(li.get_text(" ", strip=True).replace("", " "))
            continue

        if "diary" not in classes:
            continue

        meta_time_a = li.select_one("div.meta a.timeago[href^='/a/']")
        if not meta_time_a:
            continue

        href = meta_time_a.get("href", "")
        source_url = urljoin(BASE, href)
        match = re.search(r"/a/([A-Za-z0-9]+)", href)
        entry_id = match.group(1) if match else href

        time_tag = meta_time_a.select_one("time.hours")
        datetime_iso = time_tag.get("datetime", "") if time_tag else ""
        datetime_local = time_tag.get("title", "") if time_tag else ""
        time_label = clean_text(time_tag.get_text(" ", strip=True)) if time_tag else ""

        title_node = li.select_one("h4 a")
        title = clean_text(title_node.get_text(" ", strip=True)) if title_node else ""

        comment_node = li.select_one("div.line > div.comment")
        text = html_to_text_with_breaks(comment_node) if comment_node else ""

        location_node = li.select_one("div.line > span.location")
        location = ""
        if location_node:
            icon = location_node.find("i")
            if icon:
                icon.extract()
            location = clean_text(location_node.get_text(" ", strip=True))

        entries.append(
            Entry(
                id=entry_id,
                date_label=current_date,
                datetime_iso=datetime_iso,
                datetime_local=datetime_local,
                time_label=time_label,
                title=title,
                text=text,
                location=location,
                source_url=source_url,
            )
        )

    return entries


def parse_entry_datetime_parts(entry: Entry) -> Optional[Tuple[int, int, int, str]]:
    """
    解析单条日记的日期时间。
    返回：(year, month, day, HH:MM)；解析失败返回 None。
    """
    if entry.datetime_local:
        match = re.match(r"^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}:\d{2})$", entry.datetime_local)
        if match:
            return int(match.group(1)), int(match.group(2)), int(match.group(3)), match.group(4)

    date_match = re.search(r"(\d{1,2})月\s*(\d{1,2})日\s*(\d{4})", entry.date_label)
    time_match = re.search(r"(\d{1,2}):(\d{2})", entry.time_label or "")
    if date_match and time_match:
        year = int(date_match.group(3))
        month = int(date_match.group(1))
        day = int(date_match.group(2))
        hh = int(time_match.group(1))
        mm = int(time_match.group(2))
        return year, month, day, f"{hh:02d}:{mm:02d}"

    return None


def format_entry_markdown(entry: Entry, time_label: str) -> str:
    """把单条日记格式化为 Markdown 片段。"""
    lines = []
    if entry.title:
        lines.append(f"## {time_label} - {entry.title}")
    else:
        lines.append(f"## {time_label}")
    lines.append("")
    lines.append(f"- ID: `{entry.id}`")
    if entry.location:
        lines.append(f"- 地点: {entry.location}")
    lines.append(f"- 链接: {entry.source_url}")
    lines.append("")
    lines.append(entry.text.strip() if entry.text else "")
    lines.append("")
    return "\n".join(lines)


def write_split_markdown(entries: List[Entry], md_root_dir: str) -> int:
    """
    按 年/月/日 生成 Markdown 文件。
    输出路径示例：md_root_dir/2026/02/2026-02-09.md
    返回生成的 md 文件数量。
    """
    grouped: Dict[Tuple[int, int, int], List[Tuple[str, Entry]]] = {}

    for entry in entries:
        parsed = parse_entry_datetime_parts(entry)
        if not parsed:
            continue
        year, month, day, hm = parsed
        key = (year, month, day)
        grouped.setdefault(key, []).append((hm, entry))

    undated = [entry for entry in entries if parse_entry_datetime_parts(entry) is None]

    if os.path.exists(md_root_dir):
        shutil.rmtree(md_root_dir)
    os.makedirs(md_root_dir, exist_ok=True)

    file_count = 0
    for (year, month, day), items in sorted(grouped.items()):
        items.sort(key=lambda x: x[0])
        year_s = f"{year:04d}"
        month_s = f"{month:02d}"
        day_s = f"{day:02d}"

        day_dir = os.path.join(md_root_dir, year_s, month_s)
        os.makedirs(day_dir, exist_ok=True)
        day_file = os.path.join(day_dir, f"{year_s}-{month_s}-{day_s}.md")

        with open(day_file, "w", encoding="utf-8") as f:
            f.write(f"# {year_s}-{month_s}-{day_s}\n\n")
            f.write(f"> 共 {len(items)} 条日记\n\n")
            for idx, (hm, entry) in enumerate(items):
                f.write(format_entry_markdown(entry, hm))
                if idx < len(items) - 1:
                    f.write("---\n\n")

        file_count += 1

    if undated:
        with open(os.path.join(md_root_dir, "_undated.md"), "w", encoding="utf-8") as f:
            f.write("# 日期未能识别的日记\n\n")
            for entry in undated:
                f.write(format_entry_markdown(entry, entry.time_label or "时间未知"))
                f.write("---\n\n")
        file_count += 1
    return file_count


def is_login_page(html: str) -> bool:
    """判断是否被重定向回登录页。"""
    return "开始使用网页版" in html and "用户名 / Email" in html and "登入" in html


def next_posts_url(html: str, current_url: str, posts_url: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    link = soup.select_one('ul.ic-pagination a[rel="next"], [data-role="pagination"] a[data-role="load-more-trigger"]')
    if link is None:
        return None
    href = link.get("href")
    if not href:
        raise RuntimeError("下一页地址缺失；不能判断是否已完整导出")
    target = urlsplit(urljoin(current_url, href))
    expected = urlsplit(posts_url)
    if (target.scheme, target.netloc, target.path) != (expected.scheme, expected.netloc, expected.path):
        raise RuntimeError("分页地址指向意外站点或路径，已停止")
    params = dict(parse_qsl(target.query, keep_blank_values=True))
    for attr, key in (("data-load-more-page", "load_more_page"), ("data-online-bottom-time", "online_bottom_time")):
        if link.get(attr) is not None:
            params[key] = link[attr]
    return urlunsplit((target.scheme, target.netloc, target.path, urlencode(params), ""))


def scrape_all(session: requests.Session, posts_url: str, max_pages: Optional[int] = None,
               on_progress=None) -> List[Entry]:
    """Follow real continuation links, counting unique entries as they arrive."""
    all_entries = []
    seen_ids = set()
    visited_urls = set()
    url = posts_url
    page = 0
    try:
        while url:
            if url in visited_urls:
                raise RuntimeError("分页地址重复，已停止循环；本次仅导出部分日记")
            visited_urls.add(url)
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            html = resp.text
            if is_login_page(html):
                raise RuntimeError("登录会话失效，本次仅导出部分日记")
            soup = BeautifulSoup(html, "html.parser")
            if soup.select_one("ul.posts-list") is None:
                raise RuntimeError("网页日记列表结构发生变化；不能将它当成空页")
            entries = extract_entries_from_page(html)
            new_entries = [e for e in entries if e.id not in seen_ids]
            if entries and not new_entries:
                raise RuntimeError("网站返回重复日记页，已停止循环；本次仅导出部分日记")
            before_count = len(all_entries)
            for entry in new_entries:
                if entry.id not in seen_ids:
                    all_entries.append(entry)
                    seen_ids.add(entry.id)
            page += 1
            if on_progress:
                on_progress(all_entries, page)
            print(f"[第 {page} 页] 读取 {len(entries)} 条，新增 {len(all_entries) - before_count} 条，累计 {len(all_entries)} 条（已去重）", flush=True)
            if soup.select_one(".load-more-download-hint"):
                raise RuntimeError("网页提示需使用 App 查看更多；当前不是全量导出")
            next_url = next_posts_url(html, url, posts_url)
            if not entries and next_url:
                raise RuntimeError("空日记列表仍带有下一页；分页状态异常")
            if next_url is None:
                return all_entries
            if max_pages is not None and page >= max_pages:
                raise RuntimeError("已达到 max-pages 页数上限；仅导出部分日记")
            url = next_url
            time.sleep(0.3)
    except (RuntimeError, requests.RequestException) as exc:
        raise PartialExportError(str(exc), all_entries, page) from exc
    return all_entries


def atomic_write_text(path: str, text: str) -> None:
    """Replace a local file only after its entire new content has been written."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix="." + target.name + ".", delete=False) as stream:
            temporary = stream.name
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def extract_native_entries(payload: dict) -> List[Entry]:
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise RuntimeError("原生数据列表格式异常，已保留此前读取的日记")
    models = payload["models"]
    if any(not isinstance(model, dict) or not isinstance(model.get("i"), str) or not isinstance(model.get("d"), dict) for model in models):
        raise RuntimeError("原生数据模型结构异常，不能认定本页读取成功")
    photos = {m["i"]: m.get("d", {}) for m in models
              if isinstance(m, dict) and str(m.get("i", "")).startswith("3003.")}
    entries = []
    for model in models:
        if not isinstance(model, dict) or not str(model.get("i", "")).startswith("3001."):
            continue
        data = model.get("d", {})
        token = data.get("token") or model["i"].split(".", 1)[1]
        if not isinstance(token, str) or not token:
            raise RuntimeError("原生日记缺少ID，不能安全去重")
        text = data.get("comment_text")
        if not isinstance(text, str):
            pieces = []
            blocks = data.get("comment")
            if blocks is None:
                if not data.get("photos") and not data.get("special"):
                    raise RuntimeError("日记缺少可识别的正文格式；原始记录仍保留在分页备份中")
                blocks = []
            if not isinstance(blocks, list):
                raise RuntimeError("日记正文字段格式发生变化，未将它静默丢弃")
            for block in blocks:
                if isinstance(block, str):
                    pieces.append(block)
                elif isinstance(block, dict) and isinstance(block.get("t"), str):
                    pieces.append(block["t"])
                else:
                    raise RuntimeError("日记正文块出现未知格式，未将它静默导出为空白")
            text = "".join(pieces)
        local = iso = date_label = time_label = ""
        timestamp = data.get("created_at")
        if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
            try:
                dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                site_dt = dt.astimezone(timezone(timedelta(hours=8)))
                iso = dt.isoformat()
                local = site_dt.strftime("%Y-%m-%d %H:%M")
                date_label = site_dt.strftime("%Y-%m-%d")
                time_label = site_dt.strftime("%H:%M")
            except (OverflowError, OSError, ValueError):
                pass  # Keep the original timestamp and place the record in _undated.md.
        photo_items = []
        references = data.get("photos")
        if references is None:
            references = []
        if not isinstance(references, list):
            raise RuntimeError("日记图片索引格式异常；已保留此前读取的日记及原始分页")
        for reference in references:
            if isinstance(reference, str):
                key = reference if reference.startswith("3003.") else "3003." + reference
                photo_items.append(dict(photos[key]) if key in photos else {"reference": reference})
            elif isinstance(reference, dict):
                photo_items.append(reference)
            else:
                raise RuntimeError("日记图片索引包含未知记录，未将其静默丢弃")
        entries.append(Entry(
            id=token, date_label=date_label, datetime_iso=iso, datetime_local=local,
            time_label=time_label, title=data.get("title") or "", text=text,
            location=data.get("location_name") or "", source_url=f"{BASE}/a/{quote(token, safe='')}",
            privacy=data.get("privacy"), archived=bool(data.get("archived")),
            photos=photo_items, native_data=data,
        ))
    return entries


def scrape_native(client, user_token: str, metadata: dict, max_pages=None,
                  include_archived=True, on_progress=None, raw_dir=None) -> List[Entry]:
    entries = {}
    pages_fetched = 0
    metadata.setdefault("reported_counts", {})
    metadata.setdefault("collections", {})
    kinds = ("posts", "archived_posts") if include_archived else ("posts",)
    try:
        for kind in kinds:
            kind_ids = set()
            metadata["collections"][kind] = {"count": 0, "server_end_reached": False}
            for page, payload in client.iter_pages(kind, user_token, max_pages=max_pages):
                if raw_dir is not None:
                    atomic_write_text(str(Path(raw_dir) / f"{kind}_{page:04d}.json"),
                                      json.dumps(payload, ensure_ascii=False))
                batch = extract_native_entries(payload)
                for model in payload["models"]:
                    if model.get("i") == "1001." + user_token:
                        profile = model.get("d", {})
                        for key in ("posts_count", "archived_posts_count", "photos_count"):
                            value = profile.get(key)
                            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                                metadata["reported_counts"][key] = value
                before = len(entries)
                for entry in batch:
                    entries[entry.id] = entry
                    kind_ids.add(entry.id)
                pages_fetched += 1
                metadata["collections"][kind]["count"] = len(kind_ids)
                if on_progress is not None:
                    on_progress(list(entries.values()), pages_fetched)
                print(f"[{kind} 第 {page} 页] 新增 {len(entries) - before} 条，累计 {len(entries)} 条（已去重）", flush=True)
            metadata["collections"][kind]["server_end_reached"] = True
            expected_key = "posts_count" if kind == "posts" else "archived_posts_count"
            expected = metadata["reported_counts"].get(expected_key)
            if kind == "posts" and isinstance(metadata.get("web_posts_count"), int):
                web_count = metadata["web_posts_count"]
                if expected is not None and expected != web_count:
                    warning = f"网页计数 {web_count} 与原生计数 {expected} 不同；采用两者较大值保守核对，差异原因未确认"
                    metadata.setdefault("count_warnings", []).append(warning)
                expected = max(web_count, expected) if expected is not None else web_count
            metadata["collections"][kind]["expected_count"] = expected
            if expected is None:
                raise RuntimeError("原生接口未提供可核对的日记条数；不能确认全量完成")
            if len(kind_ids) != expected:
                raise RuntimeError(f"{kind} 日记数量不符：实际 {len(kind_ids)} 条，接口计数 {expected} 条")
    except (RuntimeError, ValueError, requests.RequestException) as exc:
        raise PartialExportError(str(exc), list(entries.values()), pages_fetched) from exc
    return list(entries.values())


def extract_profile_count(html: str, target_user: str) -> Optional[int]:
    soup = BeautifulSoup(html, "html.parser")
    path = urlsplit(build_posts_url(target_user)).path
    for link in soup.select("a[href]"):
        if urlsplit(urljoin(BASE, link["href"])).path == path:
            match = re.search(r"([\d,]+)\s*日记", link.get_text(" ", strip=True))
            if match:
                return int(match.group(1).replace(",", ""))
    return None


def write_outputs(entries: List[Entry], json_path: str, txt_path: str) -> None:
    records = [asdict(e) for e in entries]
    atomic_write_text(json_path, json.dumps(records, ensure_ascii=False, indent=2))
    lines = ["iCity Diary Export (text)", f"Total entries: {len(entries)}", "=" * 80, ""]
    for idx, entry in enumerate(entries, 1):
        lines.append(f"#{idx}  ID: {entry.id}")
        if entry.datetime_local:
            lines.append(f"DateTime: {entry.datetime_local}")
        elif entry.date_label or entry.time_label:
            lines.append(f"DateTime: {entry.date_label} {entry.time_label}".rstrip())
        if entry.title:
            lines.append(f"Title: {entry.title}")
        if entry.location:
            lines.append(f"Location: {entry.location}")
        lines.append(f"URL: {entry.source_url}")
        lines.extend(["Text:", entry.text or "", "-" * 80, ""])
    atomic_write_text(txt_path, "\n".join(lines))


def build_parser() -> argparse.ArgumentParser:
    """命令行参数定义。"""
    parser = argparse.ArgumentParser(
        description="导出 iCity 日记文字内容（JSON + TXT + 按日 Markdown）",
    )

    # 兼容旧用法：python icity_export.py <username> <password> [prefix]
    parser.add_argument("legacy_username", nargs="?", help="旧参数：登录用户名")
    parser.add_argument("legacy_password", nargs="?", help="旧参数：登录密码")
    parser.add_argument("legacy_prefix", nargs="?", help="旧参数：输出文件前缀")

    parser.add_argument("--source", choices=("native", "web"), default="native",
                        help="数据来源：native 手机端历史接口（默认）；web 旧网页模式")
    parser.add_argument("--no-archived", action="store_true", help="不导出封存日记（默认包含）")
    parser.add_argument("--overwrite", action="store_true", help="允许替换同名导出文件；默认拒绝覆盖")
    parser.add_argument("--username", help="登录用户名或邮箱")
    parser.add_argument("--password", help="登录密码（不推荐明文，建议留空后交互输入）")
    parser.add_argument("--target-user", help="要导出的用户 ID（默认等于登录用户名）")
    parser.add_argument("--output-dir", default=None, help="导出目录（不填会在交互里询问）")
    parser.add_argument("--prefix", help="导出文件名前缀")
    parser.add_argument("--max-pages", type=int, help="只抓前 N 页（调试用）")
    parser.add_argument("--no-split-md", action="store_true", help="不生成按年/月/日拆分的 Markdown")
    parser.add_argument("--no-interactive", action="store_true", help="禁用交互式提问（缺少参数时直接报错）")

    return parser


def fetch_web_post_count(username: str, password: str, target_user: str) -> Optional[int]:
    """Independently read the account profile counter; never print credentials."""
    with requests.Session() as session:
        login(session, username, password, target_user)
        response = session.get(f"{BASE}/u/{quote(target_user, safe='')}", timeout=30)
        response.raise_for_status()
        if is_login_page(response.text):
            raise RuntimeError("网页会话未登录，无法核对总数")
        return extract_profile_count(response.text, target_user)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    ensure_runtime_dependencies()
    if args.max_pages is not None and args.max_pages < 1:
        print("参数错误：--max-pages 必须是正整数", file=sys.stderr)
        return 2
    interactive = not args.no_interactive
    username_arg = args.username or args.legacy_username
    password_arg = args.password or args.legacy_password
    if not interactive and (not username_arg or not password_arg):
        print("参数错误：非交互模式需要账号和密码；推荐交互式隐藏输入密码", file=sys.stderr)
        return 2
    try:
        username, password = resolve_credentials(username_arg, password_arg)
        target_user = (args.target_user or username).strip()
        if args.source == "web" and interactive and not args.target_user:
            target_user = prompt_with_default("请输入个人主页用户ID（不是邮箱）", username)
        output_dir = args.output_dir
        if output_dir is None:
            output_dir = prompt_with_default("请输入导出目录", os.path.join(".", "export")) if interactive else os.path.join(".", "export")
        prefix = args.prefix or args.legacy_prefix
        if prefix is None:
            prefix = prompt_with_default("请输入导出文件名前缀", "icity_diary_export") if interactive else "icity_diary_export"
        if not prefix or prefix in {".", ".."} or "/" in prefix or "\\" in prefix:
            raise ValueError("文件名前缀不能包含路径或目录分隔符")
        if args.source == "web" and not target_user:
            raise ValueError("用户ID不能为空")
        split_md = not args.no_split_md
        if interactive and len(sys.argv) == 1 and split_md:
            split_md = prompt_yes_no("是否按 年/月/日 生成 Markdown 文件", default_yes=True)
        json_path, txt_path = build_output_paths(output_dir, prefix)
        report_path = os.path.join(output_dir, prefix + ".report.json")
        md_root_dir = os.path.join(output_dir, prefix + "_md")
        raw_dir = os.path.join(output_dir, prefix + "_raw_native")
        existing_paths = [json_path, txt_path, report_path, md_root_dir, raw_dir]
        if not args.overwrite and any(os.path.exists(path) for path in existing_paths):
            raise ValueError("已有同名备份；请换一个文件名前缀，或明确使用 --overwrite")
    except (ValueError, EOFError) as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return 2

    latest_entries = []
    metadata = {}
    report = {"status": "running", "source": args.source,
              "scope": "active_posts" if args.no_archived or args.source == "web" else "active_and_archived_posts",
              "exported_count": 0, "pages_fetched": 0, "media_files_downloaded": False,
              "metadata": metadata}

    def save_progress(entries, pages, force=False):
        nonlocal latest_entries
        latest_entries = list(entries)
        report["exported_count"] = len(latest_entries)
        report["pages_fetched"] = pages
        report["updated_at"] = datetime.now(timezone.utc).isoformat()
        if force or pages == 1 or pages % 10 == 0:
            write_outputs(latest_entries, json_path, txt_path)
            report["checkpoint_count"] = len(latest_entries)
        atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))

    exit_code = 0
    try:
        atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
        if args.source == "native":
            if os.path.exists(raw_dir):
                os.replace(raw_dir, raw_dir + ".previous-" + str(time.time_ns()))
            from icity_native import NativeClient
            client = NativeClient()
            signin = client.sign_in(username, password)
            owner = signin.get("user") or {}
            if not isinstance(owner, dict):
                raise RuntimeError("登录响应中的用户信息格式异常")
            owner_token = owner.get("token")
            actual_username = owner.get("username")
            if not isinstance(owner_token, str) or not owner_token or not isinstance(actual_username, str) or not actual_username:
                raise RuntimeError("登录响应缺少自己的用户标识，已停止以避免导出错误账户")
            if args.target_user and args.target_user.casefold() != actual_username.casefold():
                raise RuntimeError("原生模式仅导出登录账户本人的日记；目标用户与登录账户不符")
            try:
                web_count = fetch_web_post_count(username, password, actual_username)
                if web_count is not None:
                    metadata["web_posts_count"] = web_count
            except (RuntimeError, requests.RequestException):
                metadata["web_count_unavailable"] = True
                print("提示：网页总数暂时无法核对，将同时检查原生计数和服务器结束标记。", flush=True)
            password = None
            entries = scrape_native(client, owner_token, metadata, max_pages=args.max_pages,
                                    include_archived=not args.no_archived,
                                    on_progress=save_progress, raw_dir=raw_dir)
        else:
            session = requests.Session()
            session.headers.update({"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-CN,zh;q=0.9"})
            login(session, username, password, target_user)
            password = None
            profile = session.get(f"{BASE}/u/{quote(target_user, safe='')}", timeout=30)
            profile.raise_for_status()
            metadata["web_posts_count"] = extract_profile_count(profile.text, target_user)
            entries = scrape_all(session, build_posts_url(target_user), max_pages=args.max_pages,
                                 on_progress=save_progress)
            if metadata["web_posts_count"] is None or len(entries) != metadata["web_posts_count"]:
                raise PartialExportError("网页实际条数与总数不符，或无法核对总数；不能认定全量完成", entries, report["pages_fetched"])
        latest_entries = entries
        report["status"] = "finalizing"
    except PartialExportError as exc:
        latest_entries = exc.entries
        report.update(status="partial", reason=str(exc), pages_fetched=exc.pages_fetched)
        exit_code = 3
    except KeyboardInterrupt:
        report.update(status="partial", reason="用户中断；已保留成功读取的日记")
        exit_code = 130
    except (RuntimeError, requests.RequestException, ValueError) as exc:
        report.update(status="partial" if latest_entries else "failed", reason=str(exc))
        exit_code = 3 if latest_entries else 1
    except OSError as exc:
        report.update(status="partial" if latest_entries else "failed", reason="本地文件写入失败；已保留可用的原始分页")
        try:
            atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
        except OSError:
            pass
        print(f"本地文件写入失败：{exc}；请保留已生成的原始分页备份。", file=sys.stderr)
        return 1
    finally:
        password = None

    try:
        # Final reading copies are chronological; raw page snapshots preserve source order.
        latest_entries.sort(key=lambda e: (e.datetime_iso or "", e.id))
        report["photo_references"] = sum(len(e.photos) for e in latest_entries)
        save_progress(latest_entries, report["pages_fetched"], force=True)
        md_count = write_split_markdown(latest_entries, md_root_dir) if split_md else 0
        report["markdown_files"] = md_count
        if report["status"] == "finalizing":
            report["status"] = "complete"
        atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
    except (OSError, KeyboardInterrupt) as exc:
        report.update(status="partial", reason="本地文件生成中断，尚未完成全部阅读副本")
        try:
            atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
        except OSError:
            pass
        print(f"本地文件生成未完成：{exc}；已保留读取到的原始日记。", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1

    for warning in metadata.get("count_warnings", []):
        print("计数提示：" + warning)
    if report["status"] == "complete":
        print("\n所选范围的日记文字已导出，并通过数量与结束标记核对。")
    else:
        print("\n仅保存已读取部分，尚未完成全量备份。")
        print("原因：" + report.get("reason", "未知"))
    print(f"去重后条数: {len(latest_entries)}")
    print(f"JSON: {json_path}\nTXT : {txt_path}\n核对报告: {report_path}")
    if split_md:
        print(f"MD  : {md_root_dir}（{md_count} 个文件）")
    print("注意：保留了原生图片链接与元数据，但未下载图片原文件。")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
