#!/usr/bin/env python3
"""小红书 HEIF 高画质图片下载 —— 用户 2026-10-07 逆向方法。

原理：
  短链 → 带 Referer 抓笔记页 → __INITIAL_STATE__ 提 fileId
  → https://sns-img-hw.xhscdn.com/<fileId>?imageView2/2/w/0/q/100/format/heif

要点（用户实测，2026-10-07，8/8 HEIF 验证通过）：
  - format/heif 必须是斜杠！format=heif（等号）服务器直接无视，返回原生 PNG/JPEG
  - 必须带 Referer: https://www.xiaohongshu.com/，否则 403
  - 每张间隔 3 秒，否则 CDN 降级返回 PNG/JPEG
  - 下载后验 ftyp 文件头，不是 HEIF 就标 FAIL，不拿错文件凑数
  - q/100 为 imageView2 接口最高档（用户实测：q50→0.9MB、q75→2.8MB、默认→4.6MB、q100→7.5MB）

与旧版 xhs_extract_note_images.py 的区别：
  - 旧版取 CDN 裸文件原样（PNG 虚胖、JPEG 有损）
  - 本版走 imageView2 服务转 HEIF，iPhone 直出格式，体积合理、压缩更少

用法:
  python3 xhs_heif_images.py "https://xhslink.cn/o/xxx" --out-dir DIR
"""
import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone, timedelta

import requests

# 接入公共模块：智能时间戳
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.timestamps import write_timestamps_smart

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
REFERER = "https://www.xiaohongshu.com/"
EXIFTOOL = os.path.expanduser("~/workspace/tools/Image-ExifTool-13.59/exiftool")


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
    """跟短链拿到 note_id 与 xsec_token。返回 (note_id, token)。"""
    r = session.get(share_url, allow_redirects=True, timeout=30)
    full = urllib.parse.unquote(r.url)
    m = re.search(r'(?:explore/|discovery/item/)([0-9a-f]+)', full)
    if not m:
        raise ValueError(f"没拿到 note_id，短链可能失效: {full[:80]}")
    note_id = m.group(1)
    q = urllib.parse.urlparse(full).query
    token = urllib.parse.parse_qs(q).get('xsec_token', [''])[0]
    if not token:
        raise ValueError("没拿到 xsec_token")
    return note_id, token


def fetch_note_page(session: requests.Session, note_id: str, token: str,
                    max_retries: int = 3) -> str:
    """带 Referer 抓笔记页。WAF 拦截（403/429/空页）时指数退避重试。"""
    enc = urllib.parse.quote(token, safe='')
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


def extract_file_ids(html_text: str) -> list:
    """用户验证过的方法：正则提 fileId，处理 \\u002F 转义，去重保序。"""
    ids = re.findall(r'"fileId"\s*:\s*"([^"]+)"', html_text)
    ids = [i.replace('\\u002F', '/') for i in ids]
    return list(dict.fromkeys(ids))


def is_heif(content: bytes) -> bool:
    """验文件头：真 HEIF 前 12 字节含 ftyp。"""
    return len(content) >= 12 and b'ftyp' in content[:12]


def download_heif(session: requests.Session, file_id: str, out_path: str) -> dict:
    # 注意斜杠：imageView2/2/w/0/q/100/format/heif（等号版服务器无视！）
    url = f"https://sns-img-hw.xhscdn.com/{file_id}?imageView2/2/w/0/q/100/format/heif"
    r = session.get(url, timeout=60)
    r.raise_for_status()
    ok_heif = is_heif(r.content)
    good = r.status_code == 200 and len(r.content) > 10000 and ok_heif
    if good:
        with open(out_path, "wb") as f:
            f.write(r.content)
    return {
        "downloaded": good,
        "path": out_path if good else None,
        "url": url,
        "status": "original_success" if good else "failed",
        "bytes": len(r.content),
        "heif": ok_heif,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="小红书 HEIF 高画质图片下载（用户逆向方法）。")
    parser.add_argument("input", help="小红书分享文本或短链")
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    session = make_session()

    # 1. 解析短链
    print("[1/5] 解析短链...", file=sys.stderr)
    share_url = extract_share_url(args.input)
    note_id, token = resolve_short_url(session, share_url)
    print(f"  note_id={note_id} token_ok", file=sys.stderr)

    # 2. 抓笔记页
    print("[2/5] 抓笔记页面...", file=sys.stderr)
    page_html = fetch_note_page(session, note_id, token)

    # 3. 提 fileId + 作者/标题/发布时间（用于命名与时间戳）
    print("[3/5] 提取 fileId...", file=sys.stderr)
    file_ids = extract_file_ids(page_html)
    if not file_ids:
        raise SystemExit("失败：没找到 fileId，页面结构可能变了")
    print(f"  找到 {len(file_ids)} 张", file=sys.stderr)

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

    # 4+5. 下载（每张间隔 3 秒，防 CDN 降级）
    print("[4/5] 下载 HEIF 原图（每张间隔 3 秒防降级）...", file=sys.stderr)
    os.makedirs(args.out_dir, exist_ok=True)
    results = []
    for n, fid in enumerate(file_ids, 1):
        if n > 1:
            time.sleep(3)
        filename = f"{name_part}_{title_part}_{n:02d}_{date_part}.heic"
        out_path = os.path.join(args.out_dir, filename)
        try:
            result = download_heif(session, fid, out_path)
        except Exception as e:
            result = {"downloaded": False, "path": None, "status": "failed",
                      "bytes": 0, "heif": False, "error": str(e)}
        result.update({"index": n, "file_id": fid})
        fmt = "HEIF" if result.get("heif") else "非HEIF!"
        print(f"  [{n}/{len(file_ids)}] {result['bytes']//1024}KB {fmt} "
              f"{'OK' if result['downloaded'] else 'FAIL'} -> {filename}", file=sys.stderr)
        print(json.dumps(result, ensure_ascii=False))
        results.append(result)

    ok = sum(1 for r in results if r.get("downloaded"))
    print(f"[5/5] 完成 {ok}/{len(file_ids)}", file=sys.stderr)

    manifest = {
        "input_url": share_url,
        "note_id": note_id,
        "title": note.get("title") or "",
        "author": author,
        "publish_time": publish_ts,
        "publish_time_str": publish_time_str,
        "method": "imageView2/2/w/0/q/100/format/heif",
        "status": "original_success" if ok == len(file_ids) and ok > 0 else "failed",
        "images": results,
    }
    manifest_path = os.path.join(args.out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # 一步到位：自动写入时间戳
    downloaded = [r["path"] for r in results if r.get("downloaded") and r.get("path")]
    if downloaded and publish_time_str:
        write_timestamps_smart(downloaded, publish_time_str, kind="image")

    print(json.dumps({"manifest": manifest_path, "status": manifest["status"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
