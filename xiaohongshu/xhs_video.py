#!/usr/bin/env python3
"""小红书视频原画下载 —— 逆向方法。

原理：
  短链 → 抓笔记页 → __INITIAL_STATE__ 提 originVideoKey
  → https://sns-video-hw.xhscdn.com/<originVideoKey>（免签名，裸 key 直连）

要点：
  - originVideoKey 在 video.consumer 分支
  - 已验证：4K 99MB iPhone 原生 MOV，页面 MD5 与下载 MD5 一致，真原文件
  - 带 Referer（common.headers），无害且保险；实测无 Referer 也能通

用法:
  python3 xhs_video.py "https://xhslink.cn/o/xxx" --out-dir DIR
"""
import argparse
import html
import json
import os
import re
import sys
import urllib.parse
import hashlib
from datetime import datetime, timezone, timedelta

import requests

# 接入公共模块（仓库内运行时用）；单文件分发时缺 common/ 则用内置默认值
try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from common.headers import XIAOHONGSHU as COMMON_HEADERS
    from common.config import MAX_RETRIES
    from common.retry import with_retry, check_response, RetryableHTTPError
    from common.timestamps import write_timestamps_smart
    _HAS_COMMON = True
except ImportError:
    _HAS_COMMON = False
    COMMON_HEADERS = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
        "Referer": "https://www.xiaohongshu.com/",
    }
    MAX_RETRIES = 3
    class RetryableHTTPError(Exception): pass
    def with_retry(*a, **k):
        def deco(f): return f
        return deco
    def check_response(r): r.raise_for_status()
    def write_timestamps_smart(files, timestr, kind="video"): pass


def _safe_name(text: str, max_len: int = 30) -> str:
    """去除非法字符、多余符号、截断（Windows 文件名不认 \\/*?:\"<>|）"""
    text = re.sub(r'[#@&]', '', text)
    text = re.sub(r'[\\/*?:"<>|\r\n\t]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:max_len] if len(text) > max_len else text


def extract_share_url(text: str) -> str:
    match = re.search(r"https?://[^\s]+", text)
    if not match:
        raise ValueError("no URL found in input")
    return match.group(0).rstrip("。.,，")


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(COMMON_HEADERS)
    return session


def resolve_short_url(session: requests.Session, share_url: str) -> tuple:
    r = session.get(share_url, allow_redirects=True, timeout=30)
    full = urllib.parse.unquote(r.url)
    m = re.search(r'(?:explore/|discovery/item/)([0-9a-f]+)', full)
    if not m:
        raise ValueError(f"没拿到 note_id，短链可能失效: {full[:80]}")
    note_id = m.group(1)
    q = urllib.parse.urlparse(full).query
    token = urllib.parse.parse_qs(q).get('xsec_token', [''])[0]
    return note_id, token


@with_retry(max_retries=MAX_RETRIES)
def fetch_note_page(session: requests.Session, note_id: str, token: str) -> str:
    """带 Referer 抓笔记页。WAF 拦截时指数退避重试（common.retry）。"""
    enc = urllib.parse.quote(token, safe='') if token else ''
    page_url = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={enc}&xsec_source=pc_search"
    r = session.get(page_url, timeout=40)
    if len(r.text) < 10000:
        raise RetryableHTTPError(429, f"WAF empty page: status={r.status_code} size={len(r.text)}")
    return check_response(r).text


def parse_initial_state(html_text: str) -> dict:
    match = re.search(r"window\.__INITIAL_STATE__=(\{.*?\})</script>", html_text, re.S)
    if not match:
        raise ValueError("window.__INITIAL_STATE__ not found")
    raw = html.unescape(match.group(1))
    # 只替换作为值的 undefined（后跟 , 或 }），避免破坏字符串内容
    raw = re.sub(r':undefined(?=[,}])', ':null', raw)
    return json.loads(raw)


def note_from_state(state: dict, note_id: str) -> dict:
    # 新版 SSR 结构：noteData.data.noteData
    nd = state.get("noteData", {}).get("data", {}).get("noteData", {})
    if nd:
        return nd
    # 旧版结构：note.noteDetailMap
    detail_map = state.get("note", {}).get("noteDetailMap", {})
    if note_id and note_id in detail_map:
        return detail_map[note_id].get("note", {})
    if detail_map:
        return next(iter(detail_map.values())).get("note", {})
    raise ValueError("note detail not found in initial state")


def note_title(note: dict) -> str:
    """标题：优先取 desc 首行（新版 title 字段常是短标签），回退 title。"""
    desc = (note.get("desc") or "").strip().split("\n")[0].strip()
    if desc:
        return desc
    return note.get("title") or "无标题"


def note_author(note: dict) -> str:
    user = note.get("user") or {}
    return user.get("nickName") or user.get("nickname") or "未知作者"


def extract_video_key(html_text: str, note: dict | None = None) -> tuple:
    """提 originVideoKey 与页面记录的 MD5。
    MD5 优先从解析后的 note 结构取（video.media.video.md5），
    避免全页正则取第一个的运气成分；失败时回退正则。"""
    m = re.search(r'"originVideoKey"\s*:\s*"([^"]+)"', html_text)
    if not m:
        raise ValueError("没找到 originVideoKey（可能不是视频笔记）")
    key = m.group(1).replace('\\u002F', '/')
    md5 = None
    if note:
        video = note.get("video") or {}
        media = video.get("media") or {}
        vinfo = media.get("video") or {}
        md5 = vinfo.get("md5")
    if not md5:
        md5_m = re.search(r'"md5"\s*:\s*"([0-9a-f]{32})"', html_text)
        md5 = md5_m.group(1) if md5_m else None
    return key, md5


def sniff_video_ext(content: bytes) -> str:
    """按 ftyp brand 判后缀：qt（iPhone 原生）→ .mov，其他按 .mp4。注意 brand 常带空格填充（如 b'qt  '）。"""
    if len(content) >= 12 and content[4:8] == b"ftyp":
        brand = content[8:12].strip()
        if brand == b"qt":
            return ".mov"
    return ".mp4"


def main() -> None:
    parser = argparse.ArgumentParser(description="小红书视频原画下载（用户逆向方法，originVideoKey 直连）。")
    parser.add_argument("input", help="小红书分享文本或短链")
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    session = make_session()

    # 1. 解析短链
    print("[1/4] 解析短链...", file=sys.stderr)
    share_url = extract_share_url(args.input)
    note_id, token = resolve_short_url(session, share_url)
    print(f"  note_id={note_id}", file=sys.stderr)

    # 2. 抓笔记页
    print("[2/4] 抓笔记页面...", file=sys.stderr)
    page_html = fetch_note_page(session, note_id, token)

    # 3. 提 originVideoKey 与作者/标题/发布时间
    #    先解析 note 判类型，避免图文笔记误报
    print("[3/4] 提取 originVideoKey...", file=sys.stderr)
    state = parse_initial_state(page_html)
    note = note_from_state(state, note_id)
    if note.get("type") != "video":
        print("这是图文笔记，请用 xhs_img.py 下载", file=sys.stderr)
        sys.exit(1)
    key, md5_page = extract_video_key(page_html, note)
    print(f"  key={key[:50]}...", file=sys.stderr)
    if md5_page:
        print(f"  页面 MD5={md5_page}", file=sys.stderr)
    author = note_author(note)
    title = note_title(note)
    publish_ts = note.get("time")
    if publish_ts and publish_ts > 1e12:
        publish_ts = publish_ts / 1000
    tz_sh = timezone(timedelta(hours=8))
    publish_time_str = datetime.fromtimestamp(publish_ts, tz_sh).strftime("%Y:%m:%d %H:%M:%S") if publish_ts else ""
    name_part = _safe_name(author, 12)
    title_part = _safe_name(title, 18)
    date_part = publish_time_str[0:4] + publish_time_str[5:7] + publish_time_str[8:10] if publish_time_str else "nodate"

    # 4. 下载原视频（免签名裸 key 直连）
    #    流式写入磁盘（视频可达百 MB，不进内存），增量算 MD5，按 Content-Length 对账
    print("[4/4] 下载原视频...", file=sys.stderr)
    url = f"https://sns-video-hw.xhscdn.com/{key}"
    os.makedirs(args.out_dir, exist_ok=True)
    try:
        r = session.get(url, timeout=300, stream=True)
        r.raise_for_status()
        expected = int(r.headers.get("Content-Length", 0) or 0)
        md5_hasher = hashlib.md5()
        downloaded = 0
        head_buf = b""
        tmp_path = os.path.join(args.out_dir, f".tmp_{note_id[:8]}.part")
        with open(tmp_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                if len(head_buf) < 12:
                    head_buf += chunk[:12 - len(head_buf)]
                f.write(chunk)
                md5_hasher.update(chunk)
                downloaded += len(chunk)
        if expected and downloaded != expected:
            os.remove(tmp_path)
            raise ValueError(f"下载截断：{downloaded}/{expected} 字节")
        ext = sniff_video_ext(head_buf)
        filename = f"{name_part}_{title_part}_{date_part}_{note_id[:8]}_01{ext}"
        out_path = os.path.join(args.out_dir, filename)
        os.rename(tmp_path, out_path)
        print(f"  {r.status_code} {downloaded//1024//1024}MB -> {filename}", file=sys.stderr)
    except Exception as e:
        print(f"  下载失败：{e}", file=sys.stderr)
        raise SystemExit(f"失败：视频下载失败 {e}")

    # 验 MD5：对上就是服务器原始文件，没被转码过
    md5_dl = md5_hasher.hexdigest()
    md5_match = (md5_dl == md5_page) if md5_page else None
    print(f"  下载 MD5={md5_dl}", file=sys.stderr)
    if md5_match is True:
        print("  一致 ✓ 真原文件", file=sys.stderr)
    elif md5_match is False:
        print("  不一致（可能转码过）", file=sys.stderr)

    manifest = {
        "input_url": share_url,
        "note_id": note_id,
        "title": note.get("title") or "",
        "author": author,
        "publish_time": publish_ts,
        "publish_time_str": publish_time_str,
        "origin_video_key": key,
        "md5_page": md5_page,
        "md5_download": md5_dl,
        "md5_match": md5_match,
        "status": "original_success" if (md5_match in (True, None)) else "transcoded?",
        "file": out_path,
        "bytes": downloaded,
    }
    manifest_path = os.path.join(args.out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # 一步到位：自动写入时间戳
    if publish_time_str:
        write_timestamps_smart([out_path], publish_time_str, kind="video")

    print(json.dumps({"manifest": manifest_path, "status": manifest["status"],
                      "file": out_path, "md5_match": md5_match}, ensure_ascii=False))


if __name__ == "__main__":
    main()
