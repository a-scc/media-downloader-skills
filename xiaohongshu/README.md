# 小红书原图下载

- ⏰ **自动时间戳**：下载后自动用 exiftool 写入发布时间（东八区，PNG 写 XMP）
- 📝 **文件名**：`{作者}_{标题}_{序号}_{发布日期}.{后缀}`（用户 2026-10-05 规范）

## 新版管线（用户 2026-10-07 逆向，推荐）

### 图片：`xhs_img.py` —— HEIF 高画质版

```bash
python3 xhs_img.py "https://xhslink.cn/o/xxx" --out-dir DIR
```

原理：短链 → 带 Referer 抓笔记页 → `__INITIAL_STATE__` 提 fileId
→ `https://sns-img-hw.xhscdn.com/<fileId>?imageView2/2/w/0/q/100/format/heif`

要点（用户实测）：
- `format/heif` 必须是**斜杠**，`format=heif`（等号）服务器直接无视，返回原生 PNG/JPEG
- 必须带 `Referer: https://www.xiaohongshu.com/`，否则 403
- 每张间隔 3 秒，否则 CDN 降级返回 PNG/JPEG
- 下载后验 ftyp 文件头，不是 HEIF 就标 FAIL
- q/100 为 imageView2 接口最高档（q50→0.9MB、q75→2.8MB、默认→4.6MB、q100→7.5MB）

### 视频：`xhs_video.py` —— 原画直连版（新增能力）

```bash
python3 xhs_video.py "https://xhslink.cn/o/xxx" --out-dir DIR
```

原理：`__INITIAL_STATE__` 提 `originVideoKey` → `https://sns-video-hw.xhscdn.com/<key>`（免签名裸 key）。
下载后自动比对页面 MD5：一致即服务器原始文件（已验证 4K 99MB iPhone 原生 MOV）。
yt-dlp 也有此接口（`format_id='direct'`），第三方验证通过。

