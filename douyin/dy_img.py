# -*- coding: utf-8 -*-
"""抖音图文下载器（图片专用）。

下载原理：
  主链：detail API 取图片直链（被 WAF 拦时走不通）
  兜底：--urls 模式（浏览器提链 → 直接下载）

  下载 q75 档图片（抖音服务器公开最高档；q75 是有损转码，非相机原图）。
  拿不到最高档直接报错，不降级。

反爬：统一 UA + Referer（common.headers），图片间隔见 common.config，
429/403 指数退避重试（common.retry）。
"""
import sys
import os

# 接入公共模块（仓库内运行时用）；单文件分发时缺 common/ 则用内置默认值
try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from common.headers import DOUYIN as COMMON_HEADERS
    from common.config import RATE_LIMITS
    from common.timestamps import write_timestamps_smart
    HEADERS = dict(COMMON_HEADERS)
    _HAS_COMMON = True
except ImportError:
    _HAS_COMMON = False
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        "Referer": "https://www.douyin.com/",
    }
    RATE_LIMITS = {"douyin_image": 2}
    def write_timestamps_smart(files, timestr, kind="image"):
        pass  # 单文件模式：跳过时间戳写入

try:
    import requests
except ImportError:
    print("[!] 缺少 requests 库，请执行：pip install requests")
    sys.exit(1)

import re
import struct
import time
from datetime import datetime, timezone, timedelta


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
    """注册匿名 ttwid cookie（detail API 必须携带，否则返回空）。"""
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
    """从 douyin.com 的 detail JSON API 获取内容信息（视频+图文）。

    detail API 需带 ttwid cookie，首次返回空时自动注册 ttwid 后重试。
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
                # 直接取 url_list[0]（已验证 webp/jpeg 两版等价，不挑格式）
                url_list = img.get("url_list", [])
                if not url_list:
                    continue
                best_url = url_list[0]
                ext = "webp" if ".webp" in best_url else "jpg"
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
        # dy_img.py 是图片专用，遇到视频直接返回 None，外层提示用 dy_video.py
        print("[!] detail API 返回的是视频，请用 dy_video.py 下载")
        return None
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
                  expected_size: tuple = None, max_retries: int = 3,
                  single_try: bool = False) -> bool:
    """下载单个文件（视频流式+进度条，图片直接下载）。
    图片：下载后验尺寸，若小于 expected_size 则重下（抖音 CDN 档位不稳定）。
    无 expected_size 时：连下多次取尺寸最大者。
    single_try=True 时只下一次（直链模式：URL 已是浏览器提的最高档）。"""
    try:
        print(f"[+] 正在下载: {os.path.basename(filepath)}")
        if is_image:
            best_content = None
            best_size = (0, 0)
            tries = 1 if single_try else max_retries
            for attempt in range(tries):
                r = requests.get(url, headers=HEADERS, timeout=30)
                r.raise_for_status()
                content = r.content
                # Content-Length 对账：截断直接报错
                cl = r.headers.get("content-length")
                if cl and len(content) != int(cl):
                    print(f"[!] {os.path.basename(filepath)}: 内容截断 "
                          f"({len(content)}/{cl} 字节)")
                    return False
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
                if attempt < tries - 1:
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

    # ---- 主链：detail API（视频+图文）----
    # 图文 detail 已确认被 WAF 拦（四方验证），走不通时直接指引 --urls
    print("[*] 请求 detail JSON API...")
    data2 = get_detail_api(content_id)
    if data2:
        info = parse_detail_api(data2)
        if info:
            print("[+] detail API 获取成功")

    # 都失败了
    if not info:
        print()
        print("=" * 50)
        print("⚠️  下载失败：detail API 被 WAF 拦。")
        print("   请改用 --urls 模式：")
        print("   1. 用浏览器打开分享链接，从页面源码提图片直链存入 urls.txt")
        print("   2. python dy_img.py --urls urls.txt --out-dir ./out")
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


def run_urls(urls_file: str, output_dir: str = None) -> bool:
    """直链模式：从文本文件逐行读取图片直链直接下载（跳过 detail API）。

    用于 detail API 被 WAF 拦截时：先用浏览器从页面提直链存文件，再用本模式下载。
    每行一个 URL，空行和 # 开头行跳过。扩展名按 URL 中的模板判断。
    """
    if output_dir is None:
        output_dir = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(output_dir, exist_ok=True)
    if not os.path.exists(urls_file):
        print(f"[!] 找不到文件: {urls_file}")
        return False

    urls = []
    for enc in ("utf-8", "gbk", "utf-16"):
        try:
            with open(urls_file, "r", encoding=enc) as f:
                urls = [l.strip() for l in f
                        if l.strip() and not l.strip().startswith("#")]
            break
        except Exception:
            continue

    if not urls:
        print("[!] 文件中未找到有效 URL")
        return False

    print(f"[+] 直链模式，共 {len(urls)} 个 URL")
    img_interval = RATE_LIMITS.get("douyin_image", 2)
    success_count = 0
    for idx, url in enumerate(urls, 1):
        if idx > 1:
            time.sleep(img_interval)
        # 扩展名按模板来（一般是 webp/jpeg，平台强制转码）
        ext = "webp" if ".webp" in url else "jpg"
        filepath = os.path.join(output_dir, f"dy_img_{idx:02d}.{ext}")
        if download_file(url, filepath, is_image=True, single_try=True):
            success_count += 1

    print(f"\n[OK] 完成 {success_count}/{len(urls)}，保存至: {os.path.abspath(output_dir)}")
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
        print("  直链下载:  python dy_img.py --urls <urls.txt> [--out-dir <保存目录>]")
        print("             （detail API 被 WAF 拦时用：先浏览器提直链存文件，再用本模式下载）")
        print("  指定目录:  python dy_img.py <链接> <保存目录>")
        print(f"  默认保存至: {script_dir}")
        sys.exit(0)

    if args[0] == "--urls":
        if len(args) < 2:
            print("[!] --urls 需要指定 URL 文件路径")
            sys.exit(1)
        run_urls(args[1], out_dir)
    elif args[0] == "--batch":
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
