# Media Downloader Skills（三平台原图下载）

抖音 / 微博 / 小红书原图（真原画）下载脚本合集，纯 Python，无需登录。

## 目录

- `douyin/` — 抖音视频真原画 + 图文原图 + 日常视频（`story.py`，需登录 Cookie）
- `weibo/` — 微博原图（largest 档）
- `xiaohongshu/` — 小红书 CDN 源文件（HEIC/PNG/JPEG 原样）

## 通用特性

- ⏰ **自动时间戳**：下载后自动用 exiftool 写入发布时间
  - JPEG/HEIC：`-DateTimeOriginal` / `-CreateDate`
  - PNG：`-XMP:CreateDate`
  - `touch -t` 保底 mtime
  - exiftool 路径：`~/workspace/tools/Image-ExifTool-13.59/exiftool`
- 📅 **时间戳自动抓取**（东八区，格式 `2026:10:02 18:26:12`，exiftool 直接可用）：
  - 抖音：`aweme_detail.create_time`
  - 微博：`created_at` 字符串转换
  - 小红书：`note.time`（兼容秒/毫秒）

## 各平台说明

### 抖音 (`douyin/`)

- 视频：真原画优先（v0d00 URI + `ratio=default`，免登录）→ H.265 高码率 → H.264 转码，三层降级
- 图文：q75 原图（服务器最高档）
- 时间戳：`aweme_detail.create_time`

```bash
python3 douyin/download.py "<分享链接>" --out-dir /tmp/douyin
```

日常视频（需 Cookie）：
```bash
python3 douyin/story.py --out-dir /tmp/douyin_story
```

### 微博 (`weibo/`)

- 图片：`largest` 原图档，带 Referer 下载
- 时间戳：`created_at` 字符串自动转换

```bash
python3 weibo/download.py "<微博链接>" --out-dir /tmp/weibo
```

### 小红书 (`xiaohongshu/`)

- 直接拿 CDN 源文件（HEIC/PNG/JPEG 原样）
- 原理：从 `__INITIAL_STATE__` 提取裸 fileId，剥掉图片处理参数，直取对象存储源文件
- 按文件魔数判定真实后缀，不信 CDN 的 content-type
- 时间戳：`note.time`

```bash
python3 xiaohongshu/xhs_extract_note_images.py "<分享链接>" --out-dir /tmp/xhs
```

## 维护记录

- 2026-10-06：三平台合并为单一仓库；视频真原画链路；自动时间戳；H.265 优先；时区修复（东八区显式）
