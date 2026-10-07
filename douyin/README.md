# 抖音无水印下载器 (Douyin Downloader Skill)

一键下载抖音视频（无水印 MP4）和图文（原图 JPEG），纯 Python 实现，无需外部工具。

## 功能

- 🎬 **视频下载**：真原画（v0d00 URI + ratio=default，免登录）→ 降级到无水印转码版
- 🖼️ **图文下载**：原图下载（.jpeg，最高分辨率）
- 🤖 **自动识别**：自动判断链接是视频还是图文
- 📋 **批量下载**：支持从 txt 文件批量导入
- 🔄 **自动降级**：真原画 → iesdouyin 页面 → detail JSON API 三层
- ⏰ **自动时间戳**：下载后自动用 exiftool 写入发布时间（JPEG 写 EXIF，PNG 写 XMP，touch -t 保底）

## 使用方法

```bash
# 下载单个（从分享链接或分享文本）
python3 dy_video.py "https://v.douyin.com/xxxx/" --out-dir /tmp/douyin

# 批量下载
python3 dy_img.py --batch links.txt --out-dir /tmp/douyin
```

支持以下输入格式：
- 抖音短链：`https://v.douyin.com/xxxx/`
- 完整分享文本：`0.28 复制打开抖音... https://v.douyin.com/xxxx/ b@a.NJ ...`
- 长链接：`https://www.douyin.com/video/xxxx`

## 输出

- **视频**：`{标题}_{video_id}.mp4`（quality 字段标明 true_original 或 transcoded）
- **时间戳**：publish_time（Unix 秒）+ publish_time_str（"2026:01:22 18:17:00"，exiftool 直接可用）
- **图文**：保存在 `{标题}_{note_id}/` 目录，图片命名为 `{作者}_{描述}_{序号}.jpeg`

## 技术说明

1. 提取分享链接 → 301 重定向 → 获取真实 URL
2. 识别 content_type（video/note）
3. 第1层：请求 iesdouyin.com 分享页 → 解析 `window._ROUTER_DATA` JSON
4. 第2层（降级）：自动切换 `www.douyin.com/aweme/v1/web/aweme/detail/` API，缺 ttwid cookie 时自动注册后重试
5. 第0层（视频优先）：取视频页源码搜 v0d00 → `aweme.snssdk.com/aweme/v1/play/?video_id=<v0d00>&ratio=default` → 302 跳真原画直链（免登录）
6. 视频：真原画失败时降级到 play_addr 转码版
7. 图文：提取 images[] → 原图下载（优先 JPEG）
8. 时间戳：detail API 的 `aweme_detail.create_time` → 自动写入 exiftool

## 注意事项（坑点）

- **2026-08-12 起 iesdouyin SSR 接口变更**：分享页不再返回 `_ROUTER_DATA` 数据（只给壳），脚本自动走第 2 层 detail API
- **detail API 首次返回空**：原因是缺 **ttwid cookie**（不是限速），脚本已内置自动注册——POST `ttwid.bytedance.com/ttwid/union/register/` 拿 ticket → 走 ixigua callback 拿 ttwid → 带 `Cookie: ttwid=...` 重试
- 视频+图文均支持第 2 层降级；仅当 ttwid 注册后 detail API 仍为空，才是 iesdouyin 间歇性 WAF 限速（ByteDance Acrawler），等一段时间重试即可
- 依赖仅需 `requests` 库，通过 `requirements.txt` + SHA256 hash 校验安装

## 文件结构

```
douyin/
├── README.md
├── requirements.txt
├── dy_img.py           # 图文原图
├── dy_video.py         # 视频真原画
└── dy_story.py         # 日常（需 Cookie）
```

## 许可

MIT

## 日常（视频）

用 `dy_story.py`（需登录 Cookie）：

```bash
# Cookie 存到 ~/.config/douyin/cookie.txt（Stream 抓包获取，纯文本一行）
python3 douyin/dy_story.py --out-dir /tmp/douyin_story

# 指定作者
python3 douyin/dy_story.py --sec-uid <sec_uid> --out-dir /tmp/douyin_story
```

- 拿 URI：`aweme.snssdk.com/aweme/v1/story/profile/list/`（Cookie + X-Gorgon 签名）
- 下载：v0300 URI + `ratio=default`（免登录，与作品同一公式）
- Cookie 过期：接口返回空列表即过期，需重新抓包
- 时间戳：每条按各自 `create_time` 自动写入
