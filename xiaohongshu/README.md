# 小红书下载器 / Xiaohongshu Downloader

一键下载小红书图文+视频，纯 Python 实现，无需外部工具。

## 功能

- 🖼️ **图片下载**：智能原图（裸链判格式，PNG 转 HEIF q100，其余存原格式）
- 🎬 **视频下载**：真原画（originVideoKey 直连，MD5 验证）
- 🔗 **短链解析**：自动解析 xhslink.cn 短链
- ⏰ **自动时间戳**：下载后自动用 exiftool 写入发布时间（东八区）

## 使用方法

```bash
# 下载图片（单条笔记）
python3 xhs_img.py "https://xhslink.cn/o/xxx" --out-dir /tmp/xhs

# 下载视频（单条笔记）
python3 xhs_video.py "https://xhslink.cn/o/xxx" --out-dir /tmp/xhs
```

支持以下输入格式：
- 小红书短链：`https://xhslink.cn/o/xxx`
- 完整笔记链接：`https://www.xiaohongshu.com/explore/xxx`

## 输出

- **图片**：`{作者}_{标题}_{发布日期}_{note_id[:8]}_{序号}.{后缀}`（智能格式：JPEG/HEIC/PNG 按原格式）
- **视频**：`{作者}_{标题}_{发布日期}_{note_id[:8]}_01.mov`（真原画，MD5 验证）
- **时间戳**：发布时间（Unix 秒）→ exiftool 自动写入（带 +08:00 时区）

## 技术说明

1. 短链解析 → 301 重定向 → 获取 note_id
2. 带 `Referer: https://www.xiaohongshu.com/` 抓笔记页 → `__INITIAL_STATE__` 提 fileId / originVideoKey
3. 图片：`https://sns-img-hw.xhscdn.com/<fileId>` 裸链先取 → 魔数判格式 → 仅 PNG 转 `?imageView2/2/w/0/q/100/format/heif`
4. 视频：`https://sns-video-hw.xhscdn.com/<originVideoKey>`（免签名裸 key）→ 下载后比对页面 MD5
5. 单域名（华为云），无备域；图片间隔 3 秒，视频无间隔要求

## 注意事项（坑点）

- **必须带 Referer**：否则 403
- **format/heif 必须是斜杠**：`format=heif`（等号）会被服务器无视，返回原格式
- **图片间隔 3 秒**：否则 CDN 降级返回低质量
- **q/100 仅转码时生效**：裸链下载时 q 参数无效
- 依赖仅需 `requests` 库

## 文件结构

```
xiaohongshu/
├── README.md
├── requirements.txt
├── xhs_img.py      # 图片智能下载
└── xhs_video.py    # 视频原画下载
```

## 许可

MIT
