# -*- coding: utf-8 -*-
"""抖音图文原图下载器（图片专用）。

下载原理：
  1. 提取分享链接 -> 跟随301重定向 -> 获取真实URL
  2. 解析 note_id，请求 https://www.iesdouyin.com/share/note/{id}/
  3. 从 window._ROUTER_DATA JSON 中提取图文
  4. 提取所有图片原图下载（q75，服务器最高档）
  5. 降级：www.douyin.com/aweme/v1/web/aweme/detail/ API

反爬：统一 UA + Referer（common.headers），图片间隔见 common.config，
429/403 指数退避重试（common.retry）。
"""
import sys
import os
import shutil

# 接入公共模块：统一请求头 / 可配置限流 / 指数退避重试
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.headers import DOUYIN as COMMON_HEADERS
from common.config import RATE_LIMITS
from common.retry import with_retry, check_response
from common.timestamps import write_timestamps_smart

# 自动安装依赖（仅通过 requirements.txt + --require-hashes，防止供应链投毒）
try:
    import requests
except ImportError:
    import subprocess
    _req_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "requirements.txt")
    if os.path.exists(_req_file):
        print("[*] 检测到缺少 requests 库，正在通过 requirements.txt 安装（SHA256 hash 校验）...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", _req_file, "--require-hashes", "-q"],
            check=True,
        )
    else:
        print("[!] 错误：未找到 requirements.txt，无法安全安装依赖。")
        print("    请手动执行：pip install -r requirements.txt --require-hashes")
        sys.exit(1)
    import requests

import re
import json
import struct
import time
import subprocess
from datetime import datetime, timezone, timedelta

EXIFTOOL = shutil.which("exiftool") or os.path.expanduser("~/workspace/tools/Image-ExifTool-13.59/exiftool")


HEADERS = dict(COMMON_HEADERS)  # 统一请求头见 common/headers.py


def extract_url(text: str) -> str | None:
    """从分享文本中提取抖音链接（短链或长链）"""
    m = re.search(r"https?://v\.douyin\.com/[a-zA-Z0-9\-_.]+", text)
    if m:
        url = m.group(0)
        return url if url.endswith("/") else url + "/"
    m = re.search(r"https?://(?:www\.)?douyin\.com/(?:video|note)/[0-9]+", text)
    if m:
        return m.group(0)
    return None


def get_real_url(share_url: str) -> str | None:
    """跟随重定向，获取真实URL"""
    if "douyin.com/video/" in share_url or "douyin.com/note/" in share_url:
        return share_url
    try:
        r = requests.get(share_url, headers=HEADERS, allow_redirects=True, timeout=10)
        return r.url
    except Exception as e:
        print(f"[!] 获取真实链接失败: {e}")
        return None


def get_content_id(real_url: str) -> tuple[str | None, str | None]:
    """从真实 URL 中提取内容 ID 和类型（video/note），返回 (content_id, content_type)"""
    # 先尝试从 URL 路径判断类型
    if "/note/" in real_url or "share/note/" in real_url:
        content_type = "note"
        m = re.search(r"/note/([0-9]+)", real_url)
        if m:
            return m.group(1), content_type
    elif "/video/" in real_url or "share/video/" in real_url:
        content_type = "video"
        m = re.search(r"/video/([0-9]+)", real_url)
        if m:
            return m.group(1), content_type

    # 从 URL 参数提取
    for param in ["modal_id", "note_id", "item_id", "video_id"]:
        m = re.search(rf"{param}=([0-9]+)", real_url)
        if m:
            return m.group(1), "video"  # 默认按视频处理，后续自动检测

    # 兜底：提取 15 位以上数字作为 ID
    m = re.search(r"/([0-9]{15,})", real_url)
    if m:
        return m.group(1), "video"

    return None, None


def get_router_data(content_id: str, content_type: str) -> dict | None:
    """请求详情页并解析 _ROUTER_DATA JSON（自动尝试 video/note 两种路径）"""
    urls_to_try = [
        f"https://www.iesdouyin.com/share/{content_type}/{content_id}/",
        f"https://www.iesdouyin.com/share/{'note' if content_type == 'video' else 'video'}/{content_id}/",
    ]

    for url in urls_to_try:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            m = re.search(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", r.text, re.DOTALL)
            if m:
                data = json.loads(m.group(1).strip())
                if _has_item_list(data):
                    return data
        except Exception:
            continue

    print("[!] 页面中未找到有效 _ROUTER_DATA")
    return None


def get_ttwid() -> str | None:
    """注册匿名 ttwid cookie（2026-08 起 detail API 必须携带，否则返回空）。"""
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": HEADERS["User-Agent"]})
        r = s.post("https://ttwid.bytedance.com/ttwid/union/register/", json={
            "region": "cn", "aid": 1768, "needFid": False,
            "service": "www.ixigua.com", "migrate_info": {},
            "cbUrlProtocol": "https", "union": True}, timeout=15)
        url = r.json().get("redirect_url", "")
        ticket = url.split("ticket=")[1].split("&")[0]
        s.get(f"https://www.ixigua.com/ttwid/union/register/callback/?aid=1768&ticket={ticket}",
              timeout=15, allow_redirects=False)
        for c in s.cookies:
            if c.name == "ttwid":
                return c.value
    except Exception as e:
        print(f"[~] ttwid 注册失败: {e}")
    return None


def get_detail_api(content_id: str) -> dict | None:
    """降级方案：从 douyin.com 的 detail JSON API 获取内容信息（视频+图文）。

    2026-08 起 iesdouyin SSR 不再返回 item_list；detail API 需带 ttwid cookie，
    首次返回空时自动注册 ttwid 后重试。
    """
    api_url = (
        "https://www.douyin.com/aweme/v1/web/aweme/detail/"
        f"?aweme_id={content_id}"
        "&device_platform=webapp&aid=6383&channel=channel_pc_web"
    )
    detail_headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/16.0 Mobile/15E148 Safari/604.1"
        ),
        "Referer": "https://www.douyin.com/",
    }
    for attempt in range(2):
        try:
            r = requests.get(api_url, headers=detail_headers, timeout=15)
            if r.status_code == 200 and r.text.strip():
                data = r.json()
                ad = data.get("aweme_detail")
                if ad:
                    return data
                print(f"[~] detail API 无 aweme_detail（status_code={data.get('status_code')}）")
            elif attempt == 0:
                print("[~] detail API 返回为空，注册 ttwid 后重试...")
                ttwid = get_ttwid()
                if ttwid:
                    detail_headers["Cookie"] = f"ttwid={ttwid}"
                else:
                    print("[~] ttwid 注册失败，放弃")
                    return None
            else:
                print("[~] detail API 返回为空（ttwid 后仍为空）")
        except Exception as e:
            print(f"[~] detail API 请求失败: {e}")
            return None
    return None


def extract_v0d00_uri(page_html: str) -> str | None:
    """从视频页源码提取 v0d00 真原画 URI（2026-10-04 实测：公开视频免登录）。
    v0d00 是源文件 URI，配合 ratio=default 可拿真原画。"""
    # v0d00 后跟 32 位左右的 base62 字符串
    m = re.search(r'v0d00[a-zA-Z0-9_-]{20,40}', page_html)
    if m:
        return m.group(0)
    return None


def get_true_original_url(video_id: str) -> str | None:
    """获取作品视频真原画下载地址（免登录）。
    流程：取视频页源码 → 搜 v0d00 URI → 拼 aweme.snssdk.com + ratio=default。
    返回 302 跳转后的直链，或 None（失败时调用方降级到转码版）。"""
    # 1. 取视频页源码（不登录）
    page_urls = [
        f"https://www.douyin.com/video/{video_id}/",
        f"https://www.iesdouyin.com/share/video/{video_id}/",
    ]
    html = None
    for url in page_urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200 and len(r.text) > 10000:
                html = r.text
                break
        except Exception:
            continue
    if not html:
        return None

    # 2. 提取 v0d00 URI
    v0d00 = extract_v0d00_uri(html)
    if not v0d00:
        print("[~] 页面源码未找到 v0d00 URI，降级到转码版")
        return None

    # 3. 拼真原画地址（不带 Cookie，302 跳 CDN 直链）
    true_url = f"https://aweme.snssdk.com/aweme/v1/play/?video_id={v0d00}&ratio=default"
    try:
        r = requests.head(true_url, headers=HEADERS, allow_redirects=True, timeout=15)
        if r.status_code == 200:
            print(f"[+] 真原画链路成功 (v0d00={v0d00[:20]}...)")
            return r.url  # 302 后的最终 CDN 直链
    except Exception as e:
        print(f"[~] 真原画地址请求失败 ({e})，降级到转码版")
    return None


def parse_detail_api(data: dict) -> dict | None:
    """从 detail JSON API 响应中提取内容信息（视频或图文）。"""
    try:
        ad = data["aweme_detail"]
        desc = ad.get("desc", "无标题")
        nickname = ad.get("author", {}).get("nickname", "未知作者")
        # 发布时间戳（Unix 秒），显式转东八区
        publish_ts = ad.get("create_time")
        tz_sh = timezone(timedelta(hours=8))
        publish_time_str = datetime.fromtimestamp(publish_ts, tz_sh).strftime("%Y:%m:%d %H:%M:%S") if publish_ts else ""

        # ---- 图文 ----
        images = ad.get("images", [])
        if images:
            media_list = []
            for i, img in enumerate(images):
                best_url = None
                for u in img.get("url_list", []):
                    if ".jpeg?" in u or ".jpg?" in u:
                        best_url = u
                        break
                if not best_url and img.get("url_list"):
                    best_url = img["url_list"][0]
                if not best_url:
                    continue
                ext = "jpeg" if (".jpeg?" in best_url or ".jpg?" in best_url) else "webp"
                name_part = _safe_name(nickname, 12)
                desc_part = _safe_name(desc, 18)
                # 日期用 YYYYMMDD（Windows 文件名不认冒号）
                date_part = publish_time_str[0:4] + publish_time_str[5:7] + publish_time_str[8:10] if publish_time_str else "nodate"
                aweme_id = ad.get("aweme_id", "")
                filename = f"{name_part}_{desc_part}_{date_part}_{aweme_id[:8]}_{i + 1:02d}.{ext}"
                # 期望尺寸（API 自带，用于校验 CDN 档位）
                exp_w = img.get("width") or 0
                exp_h = img.get("height") or 0
                media_list.append({
                    "url": best_url,
                    "filename": filename,
                    "expected_size": (exp_w, exp_h) if exp_w and exp_h else None,
                })
            if media_list:
                return {
                    "type": "image",
                    "desc": desc,
                    "nickname": nickname,
                    "publish_time": publish_ts,
                    "publish_time_str": publish_time_str,
                    "media": media_list,
                }
            print("[!] detail API 图文未找到图片地址")
            return None

        # ---- 视频 ----
        video_info = ad.get("video", {})
        # H.265 优先（play_addr_265 码率高一截），降级到 H.264（play_addr）
        play_addr_265 = video_info.get("play_addr_265", {})
        play_addr = video_info.get("play_addr", {})
        url_list_265 = play_addr_265.get("url_list", [])
        url_list = play_addr.get("url_list", [])
        # 优先用 265 的 url_list
        if url_list_265:
            url_list = url_list_265
            print("[+] 使用 H.265 (play_addr_265) 高码率版本")

        # 优先尝试真原画（v0d00 + ratio=default，免登录）
        # 2026-10-04 实测：公开视频可用，拿不到 v0d00 时降级到转码版
        aweme_id = ad.get("aweme_id", "")
        true_url = get_true_original_url(aweme_id) if aweme_id else None
        if true_url:
            name_part = _safe_name(nickname, 12)
            desc_part = _safe_name(desc, 18)
            date_part = publish_time_str[0:4] + publish_time_str[5:7] + publish_time_str[8:10] if publish_time_str else "nodate"
            return {
                "type": "video",
                "desc": desc,
                "nickname": nickname,
                "publish_time": publish_ts,
                "publish_time_str": publish_time_str,
                "quality": "true_original",
                "media": [{
                    "url": true_url,
                    "filename": f"{name_part}_{desc_part}_{date_part}_{aweme_id[:8]}_01.mp4",
                }],
            }

        # 降级：转码版
        if not url_list:
            print("[!] detail API 中未找到视频地址")
            return None

        # 优先选 douyinvod.com 直链（已是无水印），否则取第一个
        best_url = next(
            (u for u in url_list if "douyinvod.com" in u),
            url_list[0]
        )

        name_part = _safe_name(nickname, 12)
        desc_part = _safe_name(desc, 18)
        date_part = publish_time_str[0:4] + publish_time_str[5:7] + publish_time_str[8:10] if publish_time_str else "nodate"
        return {
            "type": "video",
            "desc": desc,
            "nickname": nickname,
            "publish_time": publish_ts,
            "publish_time_str": publish_time_str,
            "quality": "transcoded",
            "media": [{
                "url": best_url,
                "filename": f"{name_part}_{desc_part}_{date_part}_{ad.get('aweme_id', '')[:8]}_01.mp4",
            }],
        }
    except Exception as e:
        print(f"[!] 解析 detail API 内容出错: {e}")
        return None


def _has_item_list(data: dict) -> bool:
    """检查 _ROUTER_DATA 中是否包含有效的 item_list"""
    try:
        loader = data.get("loaderData", {})
        for k, v in loader.items():
            if not isinstance(v, dict):
                continue
            for k2, v2 in v.items():
                if isinstance(v2, dict) and "item_list" in v2 and v2["item_list"]:
                    return True
        return False
    except Exception:
        return False


def _find_item(data: dict) -> dict | None:
    """从 _ROUTER_DATA 中挖出第一个 item"""
    loader = data.get("loaderData", {})
    for k, v in loader.items():
        if not isinstance(v, dict):
            continue
        for k2, v2 in v.items():
            if isinstance(v2, dict) and "item_list" in v2:
                items = v2["item_list"]
                if items:
                    return items[0]
    return None


def parse_content(data: dict) -> dict | None:
    """
    从 JSON 数据中提取内容信息，自动判断视频还是图文。
    返回格式：
    {
        "type": "video" | "image",
        "desc": str,
        "nickname": str,
        "media": [
            {"url": "...", "filename": "..."},
            ...
        ]
    }
    """
    try:
        item = _find_item(data)
        if not item:
            print("[!] 未找到 item")
            return None

        desc = item.get("desc", "无标题")
        nickname = item.get("author", {}).get("nickname", "未知作者")
        # 发布时间（Unix 秒），转 YYYYMMDD（Windows 文件名不认冒号）
        create_ts = item.get("create_time")
        if create_ts:
            dt = datetime.fromtimestamp(create_ts, timezone(timedelta(hours=8)))
            date_part = dt.strftime("%Y%m%d")
        else:
            date_part = "nodate"

        # 判断类型：有 images 就是图文，否则是视频
        images = item.get("images", [])
        if images:
            # ---- 图文 ----
            media_list = []
            for i, img in enumerate(images):
                # 优先选 jpeg/jpg 后缀（清晰度最高），其次第一个
                best_url = None
                for u in img.get("url_list", []):
                    if ".jpeg?" in u or ".jpg?" in u:
                        best_url = u
                        break
                if not best_url:
                    best_url = img["url_list"][0]

                ext = "jpeg" if (".jpeg?" in best_url or ".jpg?" in best_url) else "webp"
                name_part = _safe_name(nickname, 12)
                desc_part = _safe_name(desc, 18)
                filename = f"{name_part}_{desc_part}_{i + 1}_{date_part}.{ext}"
                # 期望尺寸（API 自带，用于校验 CDN 档位）
                exp_w = img.get("width") or 0
                exp_h = img.get("height") or 0
                media_list.append({
                    "url": best_url,
                    "filename": filename,
                    "expected_size": (exp_w, exp_h) if exp_w and exp_h else None,
                })

            return {
                "type": "image",
                "desc": desc,
                "nickname": nickname,
                "media": media_list,
            }
        else:
            # ---- 视频 ----
            # H.265 优先（play_addr_265），降级到 H.264（play_addr）
            video_data = item.get("video", {})
            uri_265 = video_data.get("play_addr_265", {}).get("uri")
            uri = uri_265 or video_data.get("play_addr", {}).get("uri")
            if uri_265:
                print("[+] 使用 H.265 URI")
            if not uri:
                print("[!] 未找到视频 URI")
                return None

            aweme_id = item.get("aweme_id", "")
            # 优先真原画（v0d00 + ratio=default，免登录）
            true_url = get_true_original_url(aweme_id) if aweme_id else None
            if true_url:
                return {
                    "type": "video",
                    "desc": desc,
                    "nickname": nickname,
                    "quality": "true_original",
                    "media": [{"url": true_url, "filename": f"{_safe_name(nickname, 12)}_{_safe_name(desc, 18)}_{date_part}_{aweme_id[:8]}_01.mp4"}],
                }

            # 降级：转码版
            download_url = f"https://www.douyin.com/aweme/v1/play/?video_id={uri}"
            return {
                "type": "video",
                "desc": desc,
                "nickname": nickname,
                "quality": "transcoded",
                "media": [{"url": download_url, "filename": f"{_safe_name(nickname, 12)}_{_safe_name(desc, 18)}_{date_part}_{aweme_id[:8]}_01.mp4"}],
            }

    except Exception as e:
        print(f"[!] 解析内容出错: {e}")
        return None


def _safe_name(text: str, max_len: int = 30) -> str:
    """去除非法字符、多余符号、截断"""
    # 先去掉 emoji 和特殊符号
    text = re.sub(r'[#@&]', '', text)
    # 去掉文件名非法字符
    text = re.sub(r'[\\/*?:"<>|\r\n\t]', '', text)
    # 合并多余空格并 trim
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:max_len]


def sniff_image_size(content: bytes):
    """从文件魔数识别图片尺寸（不依赖 PIL）。返回 (w, h) 或 None。"""
    # PNG: IHDR 在偏移 16 处
    if content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 24:
        w, h = struct.unpack(">II", content[16:24])
        return (w, h)
    # JPEG: 找 SOF 标记
    if content.startswith(b"\xff\xd8"):
        i = 2
        while i < len(content) - 9:
            if content[i] != 0xFF:
                i += 1
                continue
            marker = content[i + 1]
            if 0xC0 <= marker <= 0xC3:  # SOF0/1/2/3
                h = struct.unpack(">H", content[i + 5:i + 7])[0]
                w = struct.unpack(">H", content[i + 7:i + 9])[0]
                return (w, h)
            if marker == 0xD9:  # EOI
                break
            seg_len = struct.unpack(">H", content[i + 2:i + 4])[0]
            i += 2 + seg_len
    # WebP: VP8 头
    if content.startswith(b"RIFF") and len(content) >= 30 and content[8:12] == b"WEBP":
        if content[12:16] == b"VP8 " and len(content) >= 30:
            w = struct.unpack("<H", content[26:28])[0] & 0x3FFF
            h = struct.unpack("<H", content[28:30])[0] & 0x3FFF
            return (w, h)
    return None


def download_file(url: str, filepath: str, is_image: bool = False,
                  expected_size: tuple = None, max_retries: int = 3) -> bool:
    """下载单个文件（视频流式+进度条，图片直接下载）。
    图片：下载后验尺寸，若小于 expected_size 则重下（抖音 CDN 档位不稳定）。
    无 expected_size 时：连下多次取尺寸最大者。"""
    try:
        print(f"[+] 正在下载: {os.path.basename(filepath)}")
        if is_image:
            best_content = None
            best_size = (0, 0)
            for attempt in range(max_retries):
                r = requests.get(url, headers=HEADERS, timeout=30)
                r.raise_for_status()
                content = r.content
                size = sniff_image_size(content) or (0, 0)
                px = size[0] * size[1]

                # 有期望尺寸：达到即收工
                if expected_size and px >= expected_size[0] * expected_size[1]:
                    best_content = content
                    best_size = size
                    break
                # 无期望尺寸：取最大者
                if px > best_size[0] * best_size[1]:
                    best_content = content
                    best_size = size
                if attempt < max_retries - 1:
                    print(f"[~] 第{attempt + 1}次尺寸 {size[0]}x{size[1]}，重下一次…")
                    time.sleep(1)

            with open(filepath, "wb") as f:
                f.write(best_content)
            size_kb = len(best_content) / 1024
            print(f"[OK] 下载完成！保存至: {os.path.abspath(filepath)} "
                  f"({size_kb:.0f} KB, {best_size[0]}x{best_size[1]})")
        else:
            r = requests.get(url, headers=HEADERS, stream=True, timeout=30)
            if r.status_code == 403:
                print("[~] 403，去掉Referer重试...")
                h2 = HEADERS.copy()
                h2.pop("Referer", None)
                r = requests.get(url, headers=h2, stream=True, timeout=30)
            r.raise_for_status()

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
            print(f"[OK] 下载完成！保存至: {os.path.abspath(filepath)}")
        return True
    except Exception as e:
        print(f"[!] 下载失败: {e}")
        return False


def run(raw_input: str, output_dir: str = None) -> bool:
    """主流程：输入分享链接/文本，自动下载视频或图文"""
    if output_dir is None:
        output_dir = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(output_dir, exist_ok=True)

    share_url = extract_url(raw_input)
    if not share_url:
        print(f"[!] 未在输入中找到有效链接: {raw_input}")
        return False

    print(f"[+] 正在解析链接: {share_url}")
    real_url = get_real_url(share_url)
    if not real_url:
        return False

    content_id, content_type = get_content_id(real_url)
    if not content_id:
        print("[!] 无法提取内容ID")
        return False

    print(f"[+] 内容ID: {content_id} (类型: {content_type})")

    info = None

    # ---- 第0层（视频优先）：真原画直取（v0d00 + ratio=default，免登录）----
    # 不依赖 iesdouyin/detail API，WAF 拦了后面两层也能走
    if content_type == "video":
        print("[+] 尝试真原画链路 (v0d00)...")
        true_url = get_true_original_url(content_id)
        if true_url:
            # 需要 desc/nickname，走 detail API 拿元数据（只取元数据，不下载）
            data2 = get_detail_api(content_id)
            if data2:
                meta = parse_detail_api(data2)
                if meta:
                    info = {
                        "type": "video",
                        "desc": meta["desc"],
                        "nickname": meta["nickname"],
                        "publish_time": meta.get("publish_time"),
                        "publish_time_str": meta.get("publish_time_str"),
                        "quality": "true_original",
                        "media": [{
                            "url": true_url,
                            "filename": f"{_safe_name(meta['desc'])}_{content_id}.mp4",
                        }],
                    }
                    print("[+] 真原画链路成功 ✅")

    # ---- 第1层：iesdouyin 页面解析 ----
    if not info:
        data = get_router_data(content_id, content_type)

        if data:
            info = parse_content(data)

    # ---- 第2层（降级）：detail JSON API（视频+图文） ----
    if not info:
        print("[~] iesdouyin 页面解析失败，尝试降级到 detail JSON API...")
        data2 = get_detail_api(content_id)
        if data2:
            info = parse_detail_api(data2)
            if info:
                print("[~] detail API 降级成功 ✅")

    # 都失败了
    if not info:
        print()
        print("=" * 50)
        print("⚠️  下载失败：抖音 iesdouyin 触发了 WAF 限速防护。")
        print("   这是抖音服务端的间歇性限速，非脚本故障。")
        print("   建议稍等几分钟到几小时后再重试即可恢复。")
        print("=" * 50)
        return False

    if info["type"] != "image":
        print("[!] 这是视频链接，请用 dy_video.py 下载")
        return False
    content_type_cn = "图文"
    print(f"[+] {content_type_cn}: {info['desc']} | 作者: {info['nickname']}")
    print(f"[+] 共 {len(info['media'])} 个文件待下载")

    # 图文专用子目录
    if info["type"] == "image":
        safe_prefix = _safe_name(info["desc"])
        output_dir = os.path.join(output_dir, f"{safe_prefix}_{content_id}")
        os.makedirs(output_dir, exist_ok=True)

    success_count = 0
    downloaded_files = []
    img_interval = RATE_LIMITS.get("douyin_image", 2)
    for idx, media in enumerate(info["media"]):
        if idx > 0:
            time.sleep(img_interval)  # 图片间隔，防 WAF（间隔见 common/config.py）
        filepath = os.path.join(output_dir, media["filename"])
        if download_file(media["url"], filepath, is_image=True,
                         expected_size=media.get("expected_size")):
            success_count += 1
            downloaded_files.append(filepath)

    # 一步到位：自动写入时间戳
    if downloaded_files and info.get("publish_time_str"):
        write_timestamps_smart(downloaded_files, info["publish_time_str"], kind="image")

    print(f"\n[OK] 全部完成！成功 {success_count}/{len(info['media'])}，保存至: {os.path.abspath(output_dir)}")
    return success_count > 0


def run_batch(txt_file: str, output_dir: str = None) -> None:
    """批量下载：从txt文件逐行读取链接"""
    if output_dir is None:
        output_dir = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(txt_file):
        print(f"[!] 找不到文件: {txt_file}")
        return

    urls = []
    for enc in ("utf-8", "gbk", "utf-16"):
        try:
            with open(txt_file, "r", encoding=enc) as f:
                urls = [l.strip() for l in f if l.strip()]
            break
        except Exception:
            continue

    if not urls:
        print("[!] 文件中未找到有效链接")
        return

    total = len(urls)
    print(f"[+] 共 {total} 个链接，开始批量下载...")
    for i, url in enumerate(urls, 1):
        print(f"\n--- 第 {i}/{total} 个 ---")
        run(url, output_dir)


if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # 支持 --out-dir 参数（与其他两个脚本统一）
    args = sys.argv[1:]
    out_dir = script_dir
    if "--out-dir" in args:
        idx = args.index("--out-dir")
        if idx + 1 < len(args):
            out_dir = args[idx + 1]
        # 移除 --out-dir 及其值，剩下的按原逻辑处理
        args = args[:idx] + args[idx + 2:]

    if len(args) < 1:
        print("用法:")
        print("  单个下载:  python dy_img.py <图文分享链接或文本> [--out-dir <保存目录>]")
        print("  批量下载:  python dy_img.py --batch <txt文件路径> [--out-dir <保存目录>]")
        print("  指定目录:  python dy_img.py <链接> <保存目录>")
        print(f"  默认保存至: {script_dir}")
        sys.exit(0)

    if args[0] == "--batch":
        txt = args[1] if len(args) > 1 else "links.txt"
        # 兼容旧的位置参数
        if len(args) > 2 and out_dir == script_dir:
            out_dir = args[2]
        run_batch(txt, out_dir)
    else:
        link = args[0]
        # 兼容旧的位置参数
        if len(args) > 1 and out_dir == script_dir:
            out_dir = args[1]
        run(link, out_dir)
