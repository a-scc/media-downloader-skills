# 微博下载器 / Weibo Downloader

📱 一键下载微博图文+视频，纯 Python 实现，无需外部工具。

## 功能

- 🖼️ **图片下载** — 原图 .jpg，自动选最大尺寸
- 🎬 **视频下载** — 高清 .mp4（优先 HD，兜底 stream）
- 🔗 **支持所有链接格式** — 标准微博链接、fx 分享链接、移动端链接
- 📋 **批量下载** — 支持从 txt 文件批量导入
- 🍃 **零外部依赖** — 仅 `requests`，不依赖 gallery-dl/yt-dlp 等工具
- 🔒 **自包含** — 访客 cookie 自动管理，365天有效
- ⏰ **自动时间戳** — 下载后自动用 exiftool 写入发布时间（JPEG 写 EXIF，touch -t 保底 mtime）

## 使用方法

```bash
# 安装依赖
pip install requests

# 下载单条微博
python3 wb_img.py "https://weibo.com/USER/STATUS_ID"

# fx 分享链接（自动解析）
python3 wb_img.py "https://mapp.api.weibo.cn/fx/XXXX.html"

# 指定保存目录
python3 wb_img.py "https://weibo.com/USER/123456" /tmp/output

# 批量下载
python3 wb_img.py --batch links.txt --out-dir /tmp/weibo
```

## 支持的链接格式

| 格式 | 示例 | 说明 |
|------|------|------|
| 标准微博 | `https://weibo.com/USER/STATUS_ID` | ✅ 最常用 |
| 分享链接 | `https://mapp.api.weibo.cn/fx/XXXX.html` | ✅ 自动 302 重定向解析 |
| 移动端 | `https://m.weibo.cn/status/STATUS_ID` | ✅ 移动端链接 |

## 输出

- **时间戳**：publish_time_str（"2026:07:05 15:33:12"，exiftool 直接可用，自动写入文件）
示例

```
./author_name_statusid/
├── prefix_01.jpg
├── prefix_02.jpg
├── prefix_03.mp4
├── ...
```

## 技术说明

1. **访客绕过** — 自动调用 `passport.weibo.com/visitor/genvisitor2` 获取访客凭证
2. **Cookie 管理** — 首次获取后保存到 `storage/weibo_cookies.json`，365 天有效
3. **API 调用** — `weibo.com/ajax/statuses/show?id=xxx` 获取微博详情
4. **媒体解析** — 支持新版 `mix_media_info` 和旧版 `pic_ids` 两种数据格式
5. **视频兜底** — `mp4_hd_url` → `mp4_720p_mp4` → `stream_url` 三级降级

## 许可证

MIT
