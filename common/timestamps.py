# -*- coding: utf-8 -*-
"""智能时间戳：有原拍摄时间则保留，没有才写发布时间。

规则（用户 2026-10-07 定）：
- 文件已有 DateTimeOriginal（图片）/ CreateDate（视频）→ 跳过，保留原值
- 没有 → 写入发布时间（原有逻辑：exiftool + 时区 +08:00，touch -t 保底）

实测：exiftool 只改元数据段，像素 MD5 不变，画质无损。
"""
import os
import subprocess

EXIFTOOL = os.path.expanduser("~/workspace/tools/Image-ExifTool-13.59/exiftool")


def has_original_time(filepath):
    """检查文件是否已有原拍摄时间。

    读 DateTimeOriginal（图片）/ CreateDate（视频），任一有非空值即 True。
    读失败返回 False（按"没有"处理，走写入逻辑）。
    """
    try:
        r = subprocess.run(
            [EXIFTOOL, "-s", "-DateTimeOriginal", "-CreateDate", filepath],
            capture_output=True, text=True, timeout=30,
        )
        for line in (r.stdout or "").splitlines():
            name, _, val = line.partition(":")
            if name.strip() in ("DateTimeOriginal", "CreateDate") and val.strip():
                return True
        return False
    except Exception:
        return False


def _touch_mtime(filepaths, ts):
    """touch -t 保底 mtime：ts "2026:10:02 18:26:12" → "202610021826"。"""
    touch_ts = ts[0:4] + ts[5:7] + ts[8:10] + ts[11:13] + ts[14:16]
    for f in filepaths:
        subprocess.run(["touch", "-t", touch_ts, f], capture_output=True, timeout=10)


def _write_image_variant(filepaths, ts):
    """图片版：JPEG/HEIC 写 EXIF + 时区偏移，PNG 写 XMP:CreateDate（含时区）。"""
    jpg_heic = [f for f in filepaths if f.lower().endswith((".jpg", ".jpeg", ".heic"))]
    pngs = [f for f in filepaths if f.lower().endswith(".png")]
    # XMP 需要 ISO 8601 带时区格式：2026-10-02T18:26:12+08:00
    ts_xmp = ts[0:4] + "-" + ts[5:7] + "-" + ts[8:10] + "T" + ts[11:19] + "+08:00"
    if jpg_heic:
        subprocess.run([EXIFTOOL, "-overwrite_original",
                        f"-DateTimeOriginal={ts}", f"-CreateDate={ts}",
                        "-OffsetTimeOriginal=+08:00", "-OffsetTimeDigitized=+08:00"] + jpg_heic,
                       capture_output=True, timeout=60)
    if pngs:
        subprocess.run([EXIFTOOL, "-overwrite_original",
                        f"-XMP:CreateDate={ts_xmp}"] + pngs,
                       capture_output=True, timeout=60)
    _touch_mtime(filepaths, ts)


def _write_video_variant(filepaths, ts):
    """视频完整版：DateTimeOriginal + CreateDate + 时区偏移（小红书/抖音日常）。"""
    subprocess.run([EXIFTOOL, "-overwrite_original",
                    f"-DateTimeOriginal={ts}", f"-CreateDate={ts}",
                    "-OffsetTimeOriginal=+08:00", "-OffsetTimeDigitized=+08:00"] + filepaths,
                   capture_output=True, timeout=60)
    _touch_mtime(filepaths, ts)


def _write_video_simple_variant(filepaths, ts):
    """视频简化版：CreateDate + OffsetTimeDigitized（微博视频）。"""
    subprocess.run([EXIFTOOL, "-overwrite_original",
                    f"-CreateDate={ts}",
                    "-OffsetTimeDigitized=+08:00"] + filepaths,
                   capture_output=True, timeout=60)
    _touch_mtime(filepaths, ts)


def write_timestamps_smart(filepaths, publish_ts, kind="image"):
    """智能写入时间戳。

    有原拍摄时间的跳过保留，没有的才写发布时间。
    kind: "image" 图片版；"video" 视频完整版；"video_simple" 微博视频版。
    publish_ts 格式 "2026:10:02 18:26:12"（东八区，exiftool 直接可用）。
    """
    if not publish_ts or not filepaths:
        return
    to_write = []
    for f in filepaths:
        if has_original_time(f):
            print(f"[=] 已有原拍摄时间，跳过保留: {os.path.basename(f)}")
        else:
            to_write.append(f)
    if not to_write:
        return
    try:
        if kind == "image":
            _write_image_variant(to_write, publish_ts)
        elif kind == "video_simple":
            _write_video_simple_variant(to_write, publish_ts)
        else:
            _write_video_variant(to_write, publish_ts)
        print(f"[+] 时间戳已写入 ({publish_ts})")
    except Exception as e:
        print(f"[~] 时间戳写入失败: {e}")
