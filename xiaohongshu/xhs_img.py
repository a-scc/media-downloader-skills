#!/usr/bin/env python3
"""小红书高画质图片下载 —— 智能策略版（2026-10-08）。

原理：
  短链 → 带 Referer 抓笔记页 → __INITIAL_STATE__ 提 fileId
  → 先取裸链判格式 → PNG 才转 HEIF，其余直接存裸链

要点：
  - 域名主备：sns-img-hw.xhscdn.com（主）、sns-na-i1.xhscdn.com（备）；
    下载链路用 failover 代替退避（主域异常切备域）
  - 智能策略：裸链按魔数判格式；PNG → ?imageView2/2/format/heic/q/100 转 HEIF；
    HEIF/JPEG/WebP 直接存裸链，不转码（转码只有代价无收益）
  - 必须带 Referer: https://www.xiaohongshu.com/，否则 403
  - 每张间隔 3 秒，否则 CDN 降级
  - 体积校验：len(content) > 10000，防截断下载

用法:
  python3 xhs_img.py "https://xhslink.cn/o/xxx" --out-dir DIR
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
from datetime import datetime, timezone, timedelta

import requests

# 域名主备（参考抖音视频主备模式）
IMG_DOMAINS = ["sns-img-hw.xhscdn.com", "sns-na-i1.xhscdn.com"]

# 接入公共模块：统一请求头 / 可配置限流 / 指数退避重试 / 智能时间戳
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.headers import XIAOHONGSHU as COMMON_HEADERS
from common.config import RATE_LIMITS, MAX_RETRIES
from common.retry import with_retry, check_response, RetryableHTTPError
from common.timestamps import write_timestamps_smart

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
    session.headers.update(COMMON_HEADERS)
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


@with_retry(max_retries=MAX_RETRIES)
def fetch_note_page(session: requests.Session, note_id: str, token: str) -> str:
    """带 Referer 抓笔记页。WAF 拦截（403/429/空页）时指数退避重试（common.retry）。"""
    enc = urllib.parse.quote(token, safe='')
    page_url = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={enc}&xsec_source=pc_search"
    r = session.get(page_url, timeout=40)
    if len(r.text) < 10000:
        raise RetryableHTTPError(429, f"WAF empty page: status={r.status_code} size={len(r.text)}")
    return check_response(r).text


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


def detect_format(content: bytes) -> str:
    """按文件魔数判定格式，不依赖 Content-Type。
    返回: heic/jpg/png/webp/unknown"""
    if len(content) < 12:
        return "unknown"
    if content[:3] == b'\xff\xd8\xff':
        return "jpg"
    if content[:8] == b'\x89PNG\r\n\x1a\n':
        return "png"
    if content[:4] == b'RIFF' and content[8:12] == b'WEBP':
        return "webp"
    # ftyp box: 精确匹配 offset 4-8，brand 在 8-12
    if content[4:8] == b'ftyp':
        brand = content[8:12].decode('ascii', errors='ignore')
        if brand.startswith("he"):  # heic/heix/hevc/hevx
            return "heic"
        # 兼容 brand 兜底：major 为 mif1 但兼容列表含 heic（少见，安卓机可能出）
        if len(content) >= 20:
            compat = content[16:20].decode('ascii', errors='ignore')
            if "heic" in compat or "mif1" in compat or "miaf" in compat:
                # 再往后扫一段确认
                tail = content[8:32].decode('ascii', errors='ignore')
                if "heic" in tail:
                    return "heic"
    return "unknown"


def download_smart(session: requests.Session, file_id: str, out_path_base: str) -> dict:
    """智能下载：裸链判格式 → PNG 才转 HEIF，其余存裸链。
    主备域名 failover（不挂 retry 装饰器，异常由内层 except 处理后切备域）。
    返回含 method/format/domain 的结果字典。"""
    # 第一步：裸链首取（主备域名循环）
    bare_domain = None
    bare_content = None
    fmt = "unknown"
    for domain in IMG_DOMAINS:
        try:
            url = f"https://{domain}/{file_id}"
            r = session.get(url, timeout=60)
            r.raise_for_status()
            # 体积校验：防截断下载
            if len(r.content) <= 10000:
                continue
            fmt = detect_format(r.content)
            if fmt != "unknown":
                bare_domain = domain
                bare_content = r.content
                break
        except Exception:
            continue
    if bare_domain is None:
        return {
            "downloaded": False, "path": None, "status": "failed",
            "bytes": 0, "format": "unknown", "method": "failed",
            "domain": None, "url": f"https://{IMG_DOMAINS[0]}/{file_id}",
        }

    # 第二步：PNG 才转码（干净写法，同域名优先，主备切换）
    method = "bare"
    content = bare_content
    final_url = f"https://{bare_domain}/{file_id}"
    if fmt == "png":
        transcoded = False
        for t_domain in IMG_DOMAINS:
            try:
                t_url = f"https://{t_domain}/{file_id}?imageView2/2/format/heic/q/100"
                tr = session.get(t_url, timeout=60)
                tr.raise_for_status()
                if len(tr.content) <= 10000:
                    continue
                if detect_format(tr.content) == "heic":
                    fmt = "heic"
                    content = tr.content
                    method = "transcode:png->heic/q100"
                    bare_domain = t_domain  # 更新为转码成功的域名
                    final_url = t_url  # 记录实际的转码 URL
                    transcoded = True
                    break
            except Exception:
                continue
        if not transcoded:
            # 转码都失败，存裸 PNG 不丢图（裸 PNG 是服务器源文件）
            method = "bare_degraded"
            # fmt 保持 "png"，bare_domain 保持裸链成功的域名

    # 第三步：动态扩展名
    ext_map = {"heic": ".heic", "jpg": ".jpg", "png": ".png", "webp": ".webp"}
    ext = ext_map.get(fmt, ".bin")
    out_path = out_path_base + ext
    with open(out_path, "wb") as f:
        f.write(content)

    return {
        "downloaded": True,
        "path": out_path,
        "url": final_url,
        "status": "original_success",
        "bytes": len(content),
        "format": fmt,
        "method": method,
        "domain": bare_domain,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="小红书高画质图片下载（智能策略：裸链判格式，PNG 才转 HEIF）。")
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
    if note.get("type") == "video":
        print("这是视频笔记，请用 xhs_video.py 下载", file=sys.stderr)
        sys.exit(1)
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

    # 4+5. 下载（图片间隔见 common/config.py，防 CDN 降级）
    img_interval = RATE_LIMITS.get("xiaohongshu_image", 3)
    print(f"[4/5] 下载图片（智能策略，每张间隔 {img_interval} 秒防降级）...", file=sys.stderr)
    os.makedirs(args.out_dir, exist_ok=True)
    results = []
    for n, fid in enumerate(file_ids, 1):
        if n > 1:
            time.sleep(img_interval)
        filename_base = f"{name_part}_{title_part}_{date_part}_{note_id[:8]}_{n:02d}"
        out_path_base = os.path.join(args.out_dir, filename_base)
        try:
            result = download_smart(session, fid, out_path_base)
        except Exception as e:
            result = {"downloaded": False, "path": None, "status": "failed",
                      "bytes": 0, "format": "unknown", "method": "failed",
                      "domain": None, "error": str(e)}
        result.update({"index": n, "file_id": fid})
        fmt = result.get("format", "unknown").upper()
        method = result.get("method", "")
        out_name = os.path.basename(result.get("path") or filename_base)
        print(f"  [{n}/{len(file_ids)}] {result['bytes']//1024}KB {fmt} [{method}] "
              f"{'OK' if result['downloaded'] else 'FAIL'} -> {out_name}", file=sys.stderr)
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
        "method": "smart: bare-first, png->heic/q100 only",
        "domains": IMG_DOMAINS,
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
