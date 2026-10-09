# orig-dl（三平台原画下载）

抖音 / 微博 / 小红书原图（真原画）下载脚本合集，纯 Python。

## 目录结构

```
common/
  headers.py     # 各平台统一 UA + Referer
  config.py      # 可配置限流间隔
  retry.py       # 429/403 指数退避重试
douyin/
  dy_img.py      # 图文原图（q75，图文专用）
  dy_video.py    # 作品视频真原画（ratio=default，视频专用）
weibo/
  wb_img.py      # 图片 large 原图
  wb_video.py    # 视频 1440p（playback_list 最高档，转码）
xiaohongshu/
  xhs_img.py     # HEIF 高画质图片（format/heif + q100）
  xhs_video.py   # 视频原画（originVideoKey）
```

## 通用特性

- 🛡️ **反爬防护**：统一浏览器 UA + Referer，可配置限流间隔，429/403 指数退避重试
- ⏰ **自动时间戳**：下载后自动用 exiftool 写入发布时间
  - JPEG/HEIC：`-DateTimeOriginal` / `-CreateDate` + 时区偏移
  - PNG：`-XMP:CreateDate`（带时区）
  - `touch -t` 保底 mtime

## 各平台说明

### 抖音 (`douyin/`)

- 视频：真原画（`ratio=default`，v0d00 URI，免登录）
- 图文：q75（服务器最高档）

### 微博 (`weibo/`)

- 图片：`large` 原图
- 视频：1440p（`playback_list` 最高档，转码）

### 小红书 (`xiaohongshu/`)

- 图片：HEIF 高画质（`imageView2/2/w/0/format/heif` + q100，3秒间隔防降级）
- 视频：真原画（`originVideoKey`，MD5 验证）

## 用法

```bash
# 小红书图片
python3 xiaohongshu/xhs_img.py "https://xhslink.cn/o/xxx"

# 小红书视频
python3 xiaohongshu/xhs_video.py "https://xhslink.cn/o/xxx"

# 抖音
python3 douyin/dy_video.py "分享链接"
python3 douyin/dy_img.py "图文链接"

# 微博
python3 weibo/wb_img.py "https://weibo.com/..."
python3 weibo/wb_video.py "https://weibo.com/..."
```
