#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音日常（视频）原画下载
- 拿 URI：调 aweme.snssdk.com/aweme/v1/story/profile/list/（需登录 Cookie + X-Gorgon 签名）
- 下载：拿到 v0300 URI 后，与作品视频同一公式（ratio=default，免登录）
- Cookie 过期判断：接口返回空列表即过期
- 时间戳：自动用 exiftool 写入（与 dy.py 同一规范）

Cookie 获取：用户用 Stream 抓包，从请求头复制 Cookie 字符串，
存到 ~/.config/douyin/cookie.txt（或用 --cookie 参数指定文件）。
"""
import os
import sys
import json
import time
import subprocess
import argparse
from datetime import datetime, timezone, timedelta

import requests

# 复用 signer（Python 版 X-Gorgon）
sys.path.insert(0, os.path.expanduser("~/workspace/douyin-stories/src"))
try:
    from signer.gorgon import get_xgorgon
except ImportError:
    print("[!] 找不到 signer，请确认 ~/workspace/douyin-stories/src/signer/ 存在")
    sys.exit(1)

EXIFTOOL = os.path.expanduser("~/workspace/tools/Image-ExifTool-13.59/exiftool")
COOKIE_FILE = os.path.expanduser("~/.config/douyin/cookie.txt")

API_HOST = "https://aweme.snssdk.com"
STORY_LIST_API = "/aweme/v1/story/profile/list/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/16.0 Mobile/15E148 Safari/604.1"
    ),
}


def load_cookie(cookie_file=None):
    """加载 Cookie（文件或参数）"""
    path = cookie_file or COOKIE_FILE
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def fetch_story_list(cookie, sec_uid=None, count=20):
    """调 story/profile/list 拿日常列表（需登录 Cookie + 签名）"""
    params = {
        "sec_uid": sec_uid or "",
        "count": str(count),
        "cursor": "0",
    }
    # X-Gorgon 签名
    query = "&".join(f"{k}={v}" for k, v in params.items())
    try:
        xgorgon = get_xgorgon(query, "", "", cookie)
    except Exception as e:
        print(f"[!] 签名失败: {e}")
        return None

    headers = dict(HEADERS)
    headers["Cookie"] = cookie
    headers["X-Gorgon"] = xgorgon

    url = API_HOST + STORY_LIST_API
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        data = r.json()
    except Exception as e:
        print(f"[!] 接口请求失败: {e}")
        return None

    if data.get("status_code") != 0:
        print(f"[!] 接口返回错误: status_code={data.get('status_code')}")
        return None

    items = data.get("aweme_list", []) or data.get("story_list", []) or []
    if not items:
        print("[!] 返回空列表：Cookie 可能已过期，请重新抓包更新")
    return items


def extract_video_info(item):
    """从单个 story item 提取视频 URI 和元数据"""
    video = item.get("video", {})
    # 日常 URI 以 v0300 开头，优先 play_addr_265（H.265）
    uri = None
    for key in ("play_addr_265", "play_addr"):
        pa = video.get(key, {})
        # 先找 uri 字段
        if pa.get("uri"):
            uri = pa["uri"]
            break
        # 再从 url_list 反推（通常不需要）
    # 兜底：直接找 v0300
    if not uri:
        import re
        text = json.dumps(item)
        m = re.search(r'v0300[a-zA-Z0-9_-]{20,40}', text)
        if m:
            uri = m.group(0)

    if not uri:
        return None

    # 发布时间（create_time，Unix 秒）
    create_time = item.get("create_time", 0)
    tz_sh = timezone(timedelta(hours=8))
    ts_str = datetime.fromtimestamp(create_time, tz_sh).strftime("%Y:%m:%d %H:%M:%S") if create_time else ""

    return {
        "uri": uri,
        "desc": item.get("desc", "无标题")[:30],
        "create_time": create_time,
        "publish_time_str": ts_str,
        "aweme_id": item.get("aweme_id", ""),
    }


def download_true_original(uri, filepath):
    """用 ratio=default 下载真原画（免登录）"""
    url = f"{API_HOST}/aweme/v1/play/?video_id={uri}&ratio=default"
    try:
        r = requests.get(url, headers=HEADERS, stream=True, timeout=60, allow_redirects=True)
        r.raise_for_status()
        total = 0
        with open(filepath, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    total += len(chunk)
        print(f"  ✅ {os.path.basename(filepath)} ({total} bytes)")
        return True
    except Exception as e:
        print(f"  ❌ 下载失败: {e}")
        return False


def write_timestamps(filepaths, ts):
    """用 exiftool 写入时间戳（与 dy.py 同一规范，带东八区时区偏移）"""
    if not ts or not filepaths:
        return
    try:
        subprocess.run([EXIFTOOL, "-overwrite_original",
                        f"-DateTimeOriginal={ts}", f"-CreateDate={ts}",
                        "-OffsetTimeOriginal=+08:00", "-OffsetTimeDigitized=+08:00"] + filepaths,
                       capture_output=True, timeout=60)
        touch_ts = ts[0:4] + ts[5:7] + ts[8:10] + ts[11:13] + ts[14:16]
        for f in filepaths:
            subprocess.run(["touch", "-t", touch_ts, f], capture_output=True, timeout=10)
        print(f"[+] 时间戳已写入 ({ts})")
    except Exception as e:
        print(f"[~] 时间戳写入失败: {e}")


def main():
    parser = argparse.ArgumentParser(description="抖音日常（视频）原画下载")
    parser.add_argument("--sec-uid", help="作者 sec_uid（可选，不填则用 Cookie 对应的账号）")
    parser.add_argument("--cookie-file", help="Cookie 文件路径（默认 ~/.config/douyin/cookie.txt）")
    parser.add_argument("--out-dir", default=".", help="保存目录")
    parser.add_argument("--count", type=int, default=20, help="拉取数量")
    args = parser.parse_args()

    cookie = load_cookie(args.cookie_file)
    if not cookie:
        print(f"[!] 未找到 Cookie 文件: {args.cookie_file or COOKIE_FILE}")
        print("    请用 Stream 抓包获取 Cookie，存到上述路径（纯文本，一行）")
        sys.exit(1)

    print("[+] 正在拉取日常列表...")
    items = fetch_story_list(cookie, sec_uid=args.sec_uid, count=args.count)
    if items is None:
        sys.exit(1)
    if not items:
        print("[!] 没有拿到日常视频（Cookie 可能过期）")
        sys.exit(1)

    print(f"[+] 拿到 {len(items)} 条日常")
    os.makedirs(args.out_dir, exist_ok=True)

    downloaded = []
    for i, item in enumerate(items, 1):
        info = extract_video_info(item)
        if not info:
            print(f"  [{i}] 跳过：未找到视频 URI")
            continue
        filename = f"日常_{i:02d}_{info['aweme_id']}.mp4"
        filepath = os.path.join(args.out_dir, filename)
        print(f"  [{i}] {info['desc']} (v0300...)")
        if download_true_original(info["uri"], filepath):
            downloaded.append((filepath, info["publish_time_str"]))

    # 按各自时间戳写入（日常每条时间不同）
    for filepath, ts in downloaded:
        if ts:
            write_timestamps([filepath], ts)

    print(f"\n[OK] 完成 {len(downloaded)}/{len(items)}")


if __name__ == "__main__":
    main()
