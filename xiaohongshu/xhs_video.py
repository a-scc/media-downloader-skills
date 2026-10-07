#!/usr/bin/env python3
"""小红书视频原画下载 —— 用户 2026-10-07 逆向方法。

原理：
  短链 → 带 Referer 抓笔记页 → __INITIAL_STATE__ 提 originVideoKey
  → https://sns-video-bd.xhscdn.com/<originVideoKey>（免签名，裸 key 直连）

要点（用户实测，2026-10-07）：
  - originVideoKey 在 video.consumer 分支
  - 已验证：4K 99MB iPhone 原生 MOV，页面 MD5 与下载 MD5 一致，真原文件
  - yt-dlp 也有此接口（format_id='direct'），第三方验证通过
  - 必须带 Referer，否则可能被拦

用法:
  python3 xhs_video_download.py "https://xhslink.cn/o/xxx" --out-dir DIR
"""
import argparse
import html
import json
import os
import shutil
import re
import subprocess
import sys
import time
import urllib.parse
import hashlib
from datetime import datetime, timezone, timedelta

import requests

# 接入公共模块：智能时间戳
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.timestamps import write_timestamps_smart

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
REFERER = "https://www.xiaohongshu.com/"
EXIFTOOL = shutil.which("exiftool") or os.path.expanduser("~/workspace/tools/Image-ExifTool-13.59/exiftool")


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
    session.headers.update({
        "User-Agent": UA,
        "Referer": REFERER,
    })
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


def fetch_note_page(session: requests.Session, note_id: str, token: str,
                    max_retries: int = 3) -> str:
    """带 Referer 抓笔记页。WAF 拦截时指数退避重试。"""
    enc = urllib.parse.quote(token, safe='') if token else ''
    page_url = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={enc}&xsec_source=pc_search"
    last_exc = None
    for attempt in range(max_retries):
        try:
            r = session.get(page_url, timeout=40)
            if r.status_code in (403, 429) or len(r.text) < 10000:
                raise requests.HTTPError(f"WAF block/empty: {r.status_code} size={len(r.text)}")
            r.raise_for_status()
            return r.text
        except (requests.RequestException, requests.HTTPError) as e:
            last_exc = e
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"[~] 笔记页被拦 ({e})，{wait}s 后重试 ({attempt + 2}/{max_retries})…",
                      file=sys.stderr)
                time.sleep(wait)
    raise last_exc


def parse_initial_state(html_text: str) -> dict:
    match = re.search(r"window\.__INITIAL_STATE__=(\{.*?\})</script>", html_text, re.S)
    if not match:
        raise ValueError("window.__INITIAL_STATE__ not found")
    raw = html.unescape(match.group(1)).replace(":undefined", ":null")
    return json.loads(raw)


def note_from_state(state: dict, note_id: str) -> dict:
    # 新版 SSR 结构（2026-10-07 起）：noteData.data.noteData
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


def extract_video_key(html_text: str) -> tuple:
    """提 originVideoKey 与页面记录的 MD5（用户验证过的方法）。"""
    m = re.search(r'"originVideoKey"\s*:\s*"([^"]+)"', html_text)
    if not m:
        raise ValueError("没找到 originVideoKey（可能不是视频笔记）")
    key = m.group(1).replace('\\u002F', '/')
    md5_m = re.search(r'"md5"\s*:\s*"([0-9a-f]{32})"', html_text)
    return key, (md5_m.group(1) if md5_m else None)


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
    print("[3/4] 提取 originVideoKey...", file=sys.stderr)
    key, md5_page = extract_video_key(page_html)
    print(f"  key={key[:50]}...", file=sys.stderr)
    if md5_page:
        print(f"  页面 MD5={md5_page}", file=sys.stderr)

    state = parse_initial_state(page_html)
    note = note_from_state(state, note_id)
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
    print("[4/4] 下载原视频...", file=sys.stderr)
    url = f"https://sns-video-bd.xhscdn.com/{key}"
    r = session.get(url, timeout=300, stream=True)
    r.raise_for_status()
    content = r.content
    ext = sniff_video_ext(content)
    filename = f"{name_part}_{title_part}_{date_part}_{note_id[:8]}_01{ext}"
    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, filename)
    with open(out_path, "wb") as f:
        f.write(content)
    print(f"  {r.status_code} {len(content)//1024//1024}MB -> {filename}", file=sys.stderr)

    # 验 MD5：对上就是服务器原始文件，没被转码过
    md5_dl = hashlib.md5(content).hexdigest()
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
        "bytes": len(content),
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
