# 抖音无水印下载器 (Douyin Downloader Skill)

一键下载抖音视频（无水印 MP4）和图文（原图 JPEG），纯 Python 实现，无需外部工具。

## 功能

- 🎬 **视频下载**：真原画（detail API 取 v0d00/v0300 URI + ratio=default，免登录），单层无降级
- 🖼️ **图文下载**：原图下载（.jpeg，最高分辨率）
- 🤖 **自动识别**：自动判断链接是视频还是图文
- 📋 **批量下载**：支持从 txt 文件批量导入
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

- **视频**：`{标题}_{video_id}.mp4`（真原画，quality=true_original）
- **时间戳**：publish_time（Unix 秒）+ publish_time_str（"2026:01:22 18:17:00"，exiftool 直接可用）
- **图文**：保存在 `{标题}_{note_id}/` 目录，图片命名为 `{作者}_{描述}_{序号}.jpeg`

## 技术说明（2026-10-09 定案：单层真原画）

1. 提取分享链接 → 301 重定向 → 获取真实 URL
2. 识别 content_type（video/note）
3. `www.douyin.com/aweme/v1/web/aweme/detail/` API 取元数据 + 视频 URI（v0d00/v0300），缺 ttwid cookie 时自动注册后重试
4. `aweme/v1/play/?video_id={uri}&ratio=default&line=0`（主备域名 www.douyin.com / aweme.snssdk.com，免登录）→ 302 跳真原画 CDN 直链 → 下载
5. 时间戳：detail API 的 `aweme_detail.create_time` → 自动写入 exiftool
6. 失败即判 WAF，等 1 小时，不降级、不绕行

## 注意事项（坑点）

- **detail API 首次返回空**：原因是缺 **ttwid cookie**（不是限速），脚本已内置自动注册——POST `ttwid.bytedance.com/ttwid/union/register/` 拿 ticket → 走 ixigua callback 拿 ttwid → 带 `Cookie: ttwid=...` 重试
- 仅当 ttwid 注册后 detail API 仍为空，才是间歇性 WAF 限速（ByteDance Acrawler），等 1 小时再试，期间不探测
- `story_25_filter`：被服务端内容过滤的视频直接跳过（永久拦死，不重试）
- 依赖仅需 `requests` 库，通过 `requirements.txt` + SHA256 hash 校验安装

## 文件结构

```
douyin/
├── README.md
├── requirements.txt
├── dy_img.py           # 图文原图
├── dy_video.py         # 视频真原画
```

## 许可

MIT

