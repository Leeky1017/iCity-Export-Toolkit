#!/usr/bin/env python3
from __future__ import annotations
"""
iCity 日记导出脚本（非技术用户版）

你只需要准备 iCity 的登录账号和密码，脚本会自动：
1) 登录
2) 一页页抓取日记
3) 导出为 JSON + TXT 两份文件
"""

import argparse
import getpass
import json
import os
from pathlib import PureWindowsPath
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin

# 运行时依赖会在脚本内自动安装，这里先占位。
requests = None
BeautifulSoup = None

# 网站基础地址（一般不需要改）
BASE = "https://icity.ly"
LOGIN_PAGE = f"{BASE}/welcome"
LOGIN_POST = f"{BASE}/users/sign_in"
BOOTSTRAP_ENV_FLAG = "ICITY_EXPORT_BOOTSTRAPPED"
DEPENDENCY_PACKAGES = ["requests", "beautifulsoup4"]


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


def build_posts_url(target_user: str) -> str:
    """根据用户名拼出日记列表地址。"""
    return f"{BASE}/u/{target_user}/posts"


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
    """确保 requests / bs4 可用；不可用时自动安装后重跑。"""
    global requests, BeautifulSoup

    if requests is not None and BeautifulSoup is not None:
        return

    try:
        import requests as _requests
        from bs4 import BeautifulSoup as _BeautifulSoup
    except ModuleNotFoundError as exc:
        missing = (exc.name or "").split(".")[0]
        if missing not in {"requests", "bs4"}:
            raise

        if os.environ.get(BOOTSTRAP_ENV_FLAG) == "1":
            raise RuntimeError(
                "自动安装依赖后仍无法导入 requests/bs4，请检查网络或 Python 环境。"
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
    """把 HTML 文本节点转成可读的纯文本，并保留换行。"""
    for br in node.find_all("br"):
        br.replace_with("\n")
    text = node.get_text("\n", strip=True)
    lines = [ln.rstrip() for ln in text.splitlines()]
    cleaned = []
    for ln in lines:
        if ln == "":
            if cleaned and cleaned[-1] == "":
                continue
            cleaned.append("")
        else:
            cleaned.append(ln)
    return "\n".join(cleaned).strip()


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
        allow_redirects=True,
    )
    login_resp.raise_for_status()

    # 再访问一次用户主页，确认确实已登录
    probe_resp = session.get(f"{BASE}/u/{target_user}", timeout=30)
    probe_resp.raise_for_status()

    page_text = probe_resp.text
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

    return file_count


def is_login_page(html: str) -> bool:
    """判断是否被重定向回登录页。"""
    return "开始使用网页版" in html and "用户名 / Email" in html and "登入" in html


def scrape_all(session: requests.Session, posts_url: str, max_pages: Optional[int] = None) -> List[Entry]:
    """分页抓取，直到出现空页（或达到 max_pages）。"""
    all_entries: List[Entry] = []
    page = 1

    while True:
        if page == 1:
            url = posts_url
        else:
            url = f"{posts_url}?page={page}"

        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        html = resp.text

        if is_login_page(html):
            raise RuntimeError(f"抓取到第 {page} 页时会话失效，请重新运行")

        entries = extract_entries_from_page(html)
        if not entries:
            break

        all_entries.extend(entries)
        print(f"[page {page}] +{len(entries)} entries (total: {len(all_entries)})", flush=True)

        if max_pages is not None and page >= max_pages:
            break

        page += 1
        time.sleep(0.15)

    # 去重（极少数情况下分页可能重叠）
    seen = set()
    deduped: List[Entry] = []
    for entry in all_entries:
        if entry.id in seen:
            continue
        seen.add(entry.id)
        deduped.append(entry)

    return deduped


def write_outputs(entries: List[Entry], json_path: str, txt_path: str) -> None:
    """写出 JSON（结构化）和 TXT（可读文本）两份结果。"""
    with open(json_path, "w", encoding="utf-8") as json_file:
        json.dump([asdict(e) for e in entries], json_file, ensure_ascii=False, indent=2)

    with open(txt_path, "w", encoding="utf-8") as txt_file:
        txt_file.write("iCity Diary Export (text)\n")
        txt_file.write(f"Total entries: {len(entries)}\n")
        txt_file.write("=" * 80 + "\n\n")

        for idx, entry in enumerate(entries, 1):
            txt_file.write(f"#{idx}  ID: {entry.id}\n")
            if entry.datetime_local:
                txt_file.write(f"DateTime: {entry.datetime_local}\n")
            elif entry.date_label or entry.time_label:
                txt_file.write(f"DateTime: {entry.date_label} {entry.time_label}".strip() + "\n")
            if entry.title:
                txt_file.write(f"Title: {entry.title}\n")
            if entry.location:
                txt_file.write(f"Location: {entry.location}\n")
            txt_file.write(f"URL: {entry.source_url}\n")
            txt_file.write("Text:\n")
            txt_file.write((entry.text or "").strip() + "\n")
            txt_file.write("-" * 80 + "\n\n")


def build_parser() -> argparse.ArgumentParser:
    """命令行参数定义。"""
    parser = argparse.ArgumentParser(
        description="导出 iCity 日记文字内容（JSON + TXT + 按日 Markdown）",
    )

    # 兼容旧用法：python icity_export.py <username> <password> [prefix]
    parser.add_argument("legacy_username", nargs="?", help="旧参数：登录用户名")
    parser.add_argument("legacy_password", nargs="?", help="旧参数：登录密码")
    parser.add_argument("legacy_prefix", nargs="?", help="旧参数：输出文件前缀")

    parser.add_argument("--username", help="登录用户名或邮箱")
    parser.add_argument("--password", help="登录密码（不推荐明文，建议留空后交互输入）")
    parser.add_argument("--target-user", help="要导出的用户 ID（默认等于登录用户名）")
    parser.add_argument("--output-dir", default=None, help="导出目录（不填会在交互里询问）")
    parser.add_argument("--prefix", help="导出文件名前缀")
    parser.add_argument("--max-pages", type=int, help="只抓前 N 页（调试用）")
    parser.add_argument("--no-split-md", action="store_true", help="不生成按年/月/日拆分的 Markdown")
    parser.add_argument("--no-interactive", action="store_true", help="禁用交互式提问（缺少参数时直接报错）")

    return parser


def main() -> int:
    """主流程：解析参数 -> 登录 -> 抓取 -> 导出。"""
    parser = build_parser()
    args = parser.parse_args()

    # 提前准备依赖，避免用户先输入密码后又因安装依赖重跑一次。
    ensure_runtime_dependencies()

    if args.max_pages is not None and args.max_pages < 1:
        print("参数错误：--max-pages 必须是大于等于 1 的整数", file=sys.stderr)
        return 2

    username_input = args.username or args.legacy_username
    password_input = args.password or args.legacy_password
    interactive_mode = not args.no_interactive
    guided_mode = interactive_mode and len(sys.argv) == 1

    if not interactive_mode:
        if not username_input or not password_input:
            print("参数错误：禁用交互模式时，必须提供 --username 和 --password", file=sys.stderr)
            return 2

    try:
        username, password = resolve_credentials(username_input, password_input)
    except ValueError as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return 2

    if interactive_mode and not args.target_user:
        target_user = prompt_with_default("请输入要导出的用户ID", username)
    else:
        target_user = (args.target_user or username).strip()

    if not target_user:
        print("输入错误：target user 不能为空", file=sys.stderr)
        return 2

    default_output_dir = os.path.join(".", "export")
    if interactive_mode and not args.output_dir:
        output_dir = prompt_with_default("请输入导出目录", default_output_dir)
    else:
        output_dir = args.output_dir or default_output_dir

    default_prefix = f"icity_{target_user}_diary_export"
    if interactive_mode and not args.prefix and not args.legacy_prefix:
        prefix = prompt_with_default("请输入导出文件名前缀", default_prefix)
    else:
        prefix = args.prefix or args.legacy_prefix or default_prefix

    if guided_mode and not args.no_split_md:
        split_md = prompt_yes_no("是否按 年/月/日 生成 Markdown 文件", default_yes=True)
    else:
        split_md = not args.no_split_md

    json_path, txt_path = build_output_paths(output_dir, prefix)
    md_root_dir = os.path.join(output_dir, f"{prefix}_md")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
    )

    try:
        login(session, username, password, target_user)
        entries = scrape_all(
            session=session,
            posts_url=build_posts_url(target_user),
            max_pages=args.max_pages,
        )

        if not entries:
            raise RuntimeError("没有抓到任何日记，请检查账号权限或目标用户")

        write_outputs(entries, json_path, txt_path)
        md_file_count = 0
        if split_md:
            md_file_count = write_split_markdown(entries, md_root_dir)
    except requests.RequestException as exc:
        print(f"网络请求失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"导出失败：{exc}", file=sys.stderr)
        return 1

    print("\n导出完成")
    print(f"总条数: {len(entries)}")
    print(f"JSON: {json_path}")
    print(f"TXT : {txt_path}")
    if split_md:
        print(f"MD  : {md_root_dir}（共 {md_file_count} 个按日文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
