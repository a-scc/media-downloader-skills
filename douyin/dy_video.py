# -*- coding: utf-8 -*-
"""抖音作品视频真原画下载器（视频专用）。

下载原理（单层真原画，2026-10-09 定案）：
  detail API 取元数据 + 视频 URI（v0d00/v0300）
  → aweme/v1/play/?video_id={uri}&ratio=default&line=0（免登录）
  → 302 跟随拿 CDN 直链 → 下载源文件

  v0300 是新链路（4K H.265 作品也走 v0300，若若 2026-10-08 验证）。
  2026-10-09 三层对比实测（Norbb「我真的没有办法」）：
    - 第1层 iesdouyin 已死：_ROUTER_DATA 无 item_list（2026-08 起 SSR 改版），删
    - 第2层转码最佳仅 1440p/2.3Mbps，真原画 4K/41Mbps，差 18 倍，删
    - 第0层原"页面取 URI"被 JSVM bot-gated，改从 detail API 的 video.uri 取
  失败即判 WAF，等 1 小时，不降级、不绕行。

反爬：统一 UA + Referer（common.headers），
429/403 指数退避重试（common.retry）。
"""
import sys
import os
import shutil

# 接入公共模块：统一请求头 / 指数退避重试
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.headers import DOUYIN as COMMON_HEADERS
from common.config import MAX_RETRIES
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
    """主路径：从 douyin.com 的 detail JSON API 获取视频元数据与 URI。

    2026-10-09 起为唯一上游（iesdouyin 第1层已死、视频页被 JSVM bot-gated）。
    2026-08 起 detail API 需带 ttwid cookie，首次返回空时自动注册 ttwid 后重试。
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
        # 若若 2026-10-08：加 Origin + 改 Referer，否则返回 0 字节
        "Origin": "https://open.douyin.com",
        "Referer": "https://open.douyin.com/",
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


def extract_v0d00_uri(text: str) -> str | None:
    """提取/校验真原画 URI（2026-10-04 实测：公开视频免登录）。
    v0d00 是老链路源文件 URI，v0300 是新链路（4K H.265 作品也走 v0300，若若 2026-10-08 验证）。
    两者配合 ratio=default 都能拿真原画。"""
    # v0d00/v0300 后跟 32 位左右的 base62 字符串
    m = re.search(r'v0(?:d00|300)[a-zA-Z0-9_-]{20,40}', text)
    if m:
        return m.group(0)
    return None


def resolve_true_original(uri: str) -> str | None:
    """由视频 URI 解析真原画 CDN 直链（免登录）。
    拼 play/?video_id={uri}&ratio=default&line=0，302 跟随拿最终直链。
    URI 从 detail API 的 video.play_addr_265.uri（或 play_addr.uri）取，
    不再走视频页（www.douyin.com/video/ 已被 JSVM bot-gated，2026-10-09 实测）。
    主备域名：先 www.douyin.com，失败则 aweme.snssdk.com。
    返回 302 跳转后的直链，或 None。"""
    if not uri:
        return None
    play_hosts = [
        "https://www.douyin.com/aweme/v1/play/",
        "https://aweme.snssdk.com/aweme/v1/play/",
    ]
    for host in play_hosts:
        true_url = f"{host}?video_id={uri}&ratio=default&line=0"
        try:
            r = requests.head(true_url, headers=HEADERS, allow_redirects=True, timeout=15)
            if r.status_code == 200:
                print(f"[+] 真原画链路成功 ({host.split('/')[2]}, uri={uri[:20]}...)")
                return r.url  # 302 后的最终 CDN 直链
        except Exception as e:
            print(f"[~] {host.split('/')[2]} 请求失败 ({e})，试下一个")
    print("[~] 真原画主备域名均失败")
    return None


def parse_detail_api(data: dict) -> dict | None:
    """从 detail JSON API 响应中提取视频真原画信息（单层，无转码降级）。

    2026-10-09 定案：转码版（最佳 1440p/2.3Mbps）相对真原画（4K/41Mbps）
    画质差 18 倍，按用户指令移除，不再提供转码分支。
    URI 直接从 video.play_addr_265.uri（或 play_addr.uri）取，
    不再走视频页（JSVM bot-gated）。
    """
    # 若若 2026-10-08：story_25_filter 检测——服务端内容过滤，永久拦死，直接跳过
    ad_check = data.get("aweme_detail")
    fd = data.get("filter_detail", {})
    if ad_check is None and fd.get("filter_reason"):
        # filter_reason 如 "story_25_filter"，别重试、别降级、别等 WAF
        print(f"[!] 内容被过滤 ({fd.get('filter_reason')})，跳过")
        return None
    try:
        ad = data["aweme_detail"]
        desc = ad.get("desc", "无标题")
        nickname = ad.get("author", {}).get("nickname", "未知作者")
        # 发布时间戳（Unix 秒），显式转东八区
        publish_ts = ad.get("create_time")
        tz_sh = timezone(timedelta(hours=8))
        publish_time_str = datetime.fromtimestamp(publish_ts, tz_sh).strftime("%Y:%m:%d %H:%M:%S") if publish_ts else ""

        # 图文走 dy_img.py，这里直接拒
        if ad.get("images"):
            print("[!] 这是图文链接，请用 dy_img.py 下载")
            return None

        # ---- 视频：真原画唯一路径 ----
        video_info = ad.get("video", {})
        uri = (video_info.get("play_addr_265", {}).get("uri")
               or video_info.get("play_addr", {}).get("uri"))
        if not uri:
            print("[!] detail API 中未找到视频 URI")
            return None
        # 格式校验：v0d00 老链路 / v0300 新链路（4K H.265 作品）
        if not extract_v0d00_uri(uri):
            print(f"[~] URI 格式非 v0d00/v0300（{uri[:20]}...），仍尝试 ratio=default")

        aweme_id = ad.get("aweme_id", "")
        true_url = resolve_true_original(uri)
        if not true_url:
            return None

        return {
            "type": "video",
            "desc": desc,
            "nickname": nickname,
            "publish_time": publish_ts,
            "publish_time_str": publish_time_str,
            "quality": "true_original",
            "media": [{
                "url": true_url,
                "filename": f"{_safe_name(desc)}_{aweme_id}.mp4",
            }],
        }
    except Exception as e:
        print(f"[!] 解析 detail API 内容出错: {e}")
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
                  expected_size: tuple = None, max_retries: int = MAX_RETRIES) -> bool:
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
    """主流程：输入分享链接/文本，下载视频真原画（单层，无转码降级）。"""
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

    # ---- 单层：detail API 取元数据+URI → play ratio=default 取真原画 ----
    # 2026-10-09 定案：第1层 iesdouyin 已死、第2层转码画质差 18 倍，均移除。
    # 失败即判 WAF，等 1 小时，不降级、不绕行。
    print("[+] 请求 detail API 取元数据与视频 URI...")
    data = get_detail_api(content_id)
    info = parse_detail_api(data) if data else None

    if not info:
        print()
        print("=" * 50)
        print("⚠️  下载失败：抖音触发了 WAF 限速防护。")
        print("   这是抖音服务端的间歇性限速，非脚本故障。")
        print("   按规矩等 1 小时后再试，期间不探测。")
        print("=" * 50)
        return False

    print(f"[+] 视频: {info['desc']} | 作者: {info['nickname']}")
    print(f"[+] 画质: {info['quality']}")
    print(f"[+] 共 {len(info['media'])} 个文件待下载")

    success_count = 0
    downloaded_files = []
    for media in info["media"]:
        filepath = os.path.join(output_dir, media["filename"])
        if download_file(media["url"], filepath, is_image=False):
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
        print("  单个下载:  python dy_video.py <视频分享链接或文本> [--out-dir <保存目录>]")
        print("  批量下载:  python dy_video.py --batch <txt文件路径> [--out-dir <保存目录>]")
        print("  指定目录:  python dy_video.py <链接> <保存目录>")
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
