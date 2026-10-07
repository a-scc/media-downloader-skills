#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微博视频下载器（视频专用，1440p 最高档）。

原理：
  1. 提取微博链接中的 status_id
  2. 访客 cookie（~/.storage/weibo_cookies.json，不存在自动获取）
  3. weibo.com/ajax/statuses/show 拿 mix_media_info / page_info
  4. 视频地址优先取 media_info.playback_list 最高档（1440p），
     兜底 mp4_hd_url → mp4_720p_mp4 → stream_url
  5. 注意：1440p 为平台转码（encoder=Lavf60.3.100），非上传原文件，
     但已是微博能给的最高清晰度

反爬：统一 UA + Referer（common.headers），
429/403 指数退避重试（common.retry）。

用法: python3 wb_video.py <微博链接> [保存目录]
      python3 wb_video.py --batch links.txt [保存目录]
"""

import os
import shutil
import re
import sys
import json
import time
import json
import argparse
import subprocess
from datetime import datetime

import requests

# 接入公共模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.headers import WEIBO_API, WEIBO_DL
from common.config import MAX_RETRIES
from common.retry import with_retry, check_response
from common.timestamps import write_timestamps_smart

EXIFTOOL = shutil.which("exiftool") or os.path.expanduser("~/workspace/tools/Image-ExifTool-13.59/exiftool")

COOKIE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "storage", "weibo_cookies.json"
)


def _save_cookies(session):
    os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
    with open(COOKIE_FILE, "w") as f:
        json.dump(requests.utils.dict_from_cookiejar(session.cookies), f)
    print(f"[+] cookie 已保存到 {COOKIE_FILE}")


def _load_cookies(session):
    if not os.path.exists(COOKIE_FILE):
        return False
    try:
        with open(COOKIE_FILE) as f:
            jar = json.load(f)
        session.cookies.update(jar)
        if "SUB" in session.cookies:
            print("[+] 从文件加载了微博 cookie ✅")
            return True
    except Exception:
        pass
    return False


def _get_visitor_cookies(session):
    """绕过新浪访客系统，获取 SUB/SUBP cookie（有效期 ~365 天）。"""
    print("[*] 正在获取访客 cookie...")
    r = session.post(
        "https://passport.weibo.com/visitor/genvisitor2",
        data={
            "cb": "visitor_gray_callback",
            "ver": "20250916",
            "tid": "",
            "from": "weibo",
            "webdriver": "false",
            "return_url": "https://weibo.com/",
        },
        headers={"Referer": "https://weibo.com/"},
    )
    m = re.search(r'\((.*?)\);\s*$', r.text)
    if not m:
        print("[!] 访客系统绕过失败")
        return False
    vd = json.loads(m.group(1)).get("data", {})
    if not vd.get("tid"):
        print("[!] 未获取到 tid")
        return False
    session.get(
        "https://passport.weibo.com/visitor/visitor",
        params={
            "a": "crossdomain",
            "t": vd["tid"],
            "sp": vd["subp"],
            "s": vd["sub"],
            "from": "weibo",
            "_rand": str(abs(hash(str(vd))))[:8],
            "url": "https://weibo.com/",
        },
        allow_redirects=False,
    )
    if "SUB" in session.cookies:
        print("[+] 访客 cookie 获取成功 ✅")
        _save_cookies(session)
        return True
    print("[!] 获取 cookie 失败")
    return False


def _ensure_cookies(session):
    if "SUB" in session.cookies and "SUBP" in session.cookies:
        return True
    if _load_cookies(session):
        return True
    return _get_visitor_cookies(session)


def resolve_fx_url(url, session=None):
    s = session or requests.Session()
    try:
        r = s.get(url, headers=WEIBO_API, allow_redirects=False, timeout=10)
        if r.status_code in (301, 302) and "location" in r.headers:
            real_url = r.headers["location"]
            print(f"  🔗 分享链接 → {real_url}")
            return real_url
        r = s.get(url, headers=WEIBO_API, timeout=10)
        m = re.search(r'return_url\s*=\s*"([^"]+)"', r.text)
        if m:
            print(f"  🔗 分享链接 → {m.group(1)}")
            return m.group(1)
        return url
    except Exception as e:
        print(f"[!] 解析分享链接失败: {e}")
        return url


def extract_status_id(url):
    patterns = [
        r'weibo\.com/\d+/([a-zA-Z0-9]+)',
        r'weibo\.(?:com|cn)/detail/(\d+)',
        r'm\.weibo\.cn/(?:status|detail)/(\d+)',
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def _safe_name(text, max_len=30):
    text = re.sub(r'[\\/*?:"<>|\r\n\t]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:max_len]


def _pick_best_video_url(mi):
    """从 media_info 选最高档视频地址。

    优先 playback_list（按 label 找 1440p，否则取第一档），
    兜底 mp4_hd_url → mp4_720p_mp4 → stream_url。
    返回 (url, quality_label)。
    """
    pb = mi.get("playback_list") or []
    if pb:
        # 先找 1440p
        for item in pb:
            label = str(item.get("label", ""))
            if "1440" in label:
                url = item.get("play_url") or item.get("url") or ""
                if url:
                    return url, "1440p"
        # 兜底：第一档（通常已按清晰度降序）
        first = pb[0]
        url = first.get("play_url") or first.get("url") or ""
        if url:
            return url, str(first.get("label", "playback"))
    url = mi.get("mp4_hd_url") or mi.get("mp4_720p_mp4") or mi.get("stream_url") or ""
    return url, "720p_fallback"


@with_retry(max_retries=MAX_RETRIES)
def fetch_status_videos(session, status_id):
    """获取单条微博的视频数据（仅视频，1440p 优先）。返回 dict。"""
    r = session.get(
        f"https://weibo.com/ajax/statuses/show?id={status_id}&isGetLongText=true",
        headers=WEIBO_API,
        timeout=15,
    )
    check_response(r)
    if not r.text:
        return {"success": False, "error": "API 返回为空"}
    try:
        data = r.json()
    except json.JSONDecodeError:
        return {"success": False, "error": "API 返回非 JSON"}
    if data.get("ok") != 1:
        return {"success": False, "error": data.get("msg") or "未知错误"}

    user = data.get("user", {})
    author = user.get("screen_name", "未知")
    text_raw = data.get("text_raw", "")
    prefix = _safe_name(text_raw or author, 20)

    files = []
    # 新版 mix_media_info（仅视频）
    mix = data.get("mix_media_info")
    if mix:
        for item in mix.get("items", []):
            if item.get("type") != "video":
                continue
            mi = item.get("data", {}).get("media_info", {})
            url, quality = _pick_best_video_url(mi)
            if url:
                files.append({"url": url, "type": "video", "quality": quality})
    # 旧版 gif 视频兜底
    if not files:
        for pic_id in data.get("pic_ids", []):
            pic = data.get("pic_infos", {}).get(pic_id, {})
            if pic.get("type") == "gif" and pic.get("video"):
                files.append({"url": pic["video"], "type": "video", "quality": "gif"})
    # 独立 page_info 视频
    pi = data.get("page_info", {})
    if pi.get("type") == "video":
        mi = pi.get("media_info", {})
        url, quality = _pick_best_video_url(mi)
        if url and not any(f["url"] == url for f in files):
            files.append({"url": url, "type": "video", "quality": quality})

    created_at = data.get("created_at", "")
    publish_time_str = ""
    if created_at:
        try:
            dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
            publish_time_str = dt.strftime("%Y:%m:%d %H:%M:%S")
        except ValueError:
            pass
    date_part = (publish_time_str[0:4] + publish_time_str[5:7] + publish_time_str[8:10]
                 if publish_time_str else "nodate")

    for i, f in enumerate(files, 1):
        f["filename"] = f"{prefix}_{date_part}_{status_id[:8]}_{i:02d}.mp4"

    return {
        "success": True,
        "author": author,
        "text": text_raw,
        "id": status_id,
        "publish_time_str": publish_time_str,
        "files": files,
    }


@with_retry(max_retries=MAX_RETRIES)
def download_file(session, url, filepath):
    """下载单个视频文件（流式+进度，带 429/403 重试）。"""
    print(f"  ⬇ {os.path.basename(filepath)}")
    r = session.get(url, headers=WEIBO_DL, stream=True, timeout=120)
    check_response(r)
    total = int(r.headers.get("content-length", 0))
    downloaded = 0
    with open(filepath, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    pct = downloaded / total * 100
                    bar = "#" * int(pct / 2)
                    print(f"\r  [{bar:<50}] {pct:.1f}%", end="", flush=True)
    print()
    size_mb = downloaded / 1024 / 1024
    print(f"  ✅ {os.path.basename(filepath)} ({size_mb:.1f} MB)")
    return True


def download(url, output_dir=None):
    """主入口：下载单条微博的视频（1440p 优先）。"""
    if output_dir is None:
        output_dir = os.getcwd()
    os.makedirs(output_dir, exist_ok=True)

    session = requests.Session()
    session.headers.update(WEIBO_API)
    original_url = url
    if "mapp.api.weibo.cn/fx/" in url:
        url = resolve_fx_url(url, session)
    status_id = extract_status_id(url)
    if not status_id:
        print(f"[!] 无法提取微博 ID: {original_url}")
        return {"success": False}

    print(f"[+] 平台: 微博视频")
    print(f"[+] Status ID: {status_id}")
    if not _ensure_cookies(session):
        print("[!] 无法获取微博访问凭证")
        return {"success": False}

    result = fetch_status_videos(session, status_id)
    if not result["success"]:
        print(f"[!] 获取微博失败: {result.get('error')}")
        return result

    print(f"[+] 作者: {result['author']}")
    files = result["files"]
    if not files:
        print("[!] 未找到视频（该微博可能只有图片，请用 wb_img.py）")
        return {"success": False, "files": 0, "dir": output_dir}

    for f in files:
        print(f"[+] 画质档: {f.get('quality', '?')}")

    author_dir = _safe_name(result["author"], 20)
    save_dir = os.path.join(output_dir, f"{author_dir}_{status_id}")
    os.makedirs(save_dir, exist_ok=True)

    downloaded_files = []
    for f in files:
        filepath = os.path.join(save_dir, f["filename"])
        try:
            if download_file(session, f["url"], filepath):
                downloaded_files.append(filepath)
        except Exception as e:
            print(f"  ❌ {f['filename']}: {e}")

    if downloaded_files and result.get("publish_time_str"):
        write_timestamps_smart(downloaded_files, result["publish_time_str"], kind="video_simple")

    print(f"\n[OK] 完成！成功 {len(downloaded_files)}/{len(files)}")
    print(f"📂 {save_dir}")
    return {"success": len(downloaded_files) > 0, "files": len(downloaded_files), "dir": save_dir}


def main():
    ap = argparse.ArgumentParser(description="微博视频下载器（1440p 最高档）")
    ap.add_argument("url", nargs="?", help="微博链接")
    ap.add_argument("output_dir", nargs="?", default=None, help="保存目录")
    ap.add_argument("--batch", dest="batch_file", default=None, help="批量下载 txt")
    ap.add_argument("--out-dir", dest="out_dir", default=None)
    args = ap.parse_args()

    out_dir = args.out_dir or args.output_dir
    if args.batch_file:
        if not os.path.exists(args.batch_file):
            print(f"❌ 批处理文件不存在: {args.batch_file}")
            return
        with open(args.batch_file, encoding="utf-8") as f:
            urls = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        print(f"📋 批量下载: {len(urls)} 个链接\n")
        for i, u in enumerate(urls, 1):
            print(f"[{i}/{len(urls)}] ", end="")
            download(u, out_dir)
            print()
    elif args.url:
        download(args.url, out_dir)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
