# 小红书原图下载

- ⏰ **自动时间戳**：下载后自动用 exiftool 写入发布时间

# xiaohongshu-original-images

用于 **提取并下载小红书笔记原图** 的 OpenClaw Skill。

核心目标：

- 优先获取原图
- 尽量避免水印图 / 预览图 / 压缩图
- 减少浏览器往返
- 提高下载稳定性

---

## 1. 部署到 OpenClaw

### 方式一：直接放到本地 skills 目录

把整个技能目录放到你的 OpenClaw skills 目录下，例如：

```text
~/.openclaw/workspace/skills/xiaohongshu-original-images
```

目录结构应保持如下：

```text
xiaohongshu-original-images/
├─ SKILL.md
├─ references/
│  └─ url-patterns.md
└─ scripts/
   └─ xhs_download_images.py
```

放好后，OpenClaw 在匹配到相关请求时就可以触发这个 skill。

---

### 方式二：作为仓库拉到你的工作区

如果你想从 GitHub 拉取：

```bash
git clone https://github.com/phylis7/xiaohongshu-original-images-skill.git
```

然后把其中的技能目录放到你的 OpenClaw skills 目录下：

```text
xiaohongshu-original-images-skill/xiaohongshu-original-images
```

复制到：

```text
~/.openclaw/workspace/skills/
```

最终目标仍然是：

```text
~/.openclaw/workspace/skills/xiaohongshu-original-images
```

---

## 2. 触发方式

当用户发送小红书链接，并表达类似意图时适用：

- 帮我下载这个小红书笔记的原图
- 把这个小红书链接里的图片原图发我
- 保存这个小红书帖子里的原图

---

## 3. 工作原理

Skill 的优先流程：

1. 打开小红书分享链接
2. 跳转到最终笔记页 `/explore/<note_id>`
3. 一次性提取：
   - `source_note_id`
   - `xsec_token`
   - `xsec_source`
   - SSR 图片信息
   - feed 返回结果
4. 优先使用 `feedRawKeys`
5. feed 不可用时，退回 `ssrRawKeys`
6. 调用下载脚本生成原图文件

---

## 4. 直接使用脚本

脚本位置：

```text
scripts/xhs_download_images.py
```

### 探测链接是否可还原为原图

```bash
python scripts/xhs_download_images.py --probe-only --url "<图片链接>"
```

### 按 raw_key 下载

```bash
python scripts/xhs_download_images.py --out-dir tmp/xhs --key "<raw_key_1>" --key "<raw_key_2>"
```

### 按 URL 直接下载

```bash
python scripts/xhs_download_images.py --out-dir tmp/xhs --url "<preview_or_raw_url>"
```

---

## 5. 文件说明

- `SKILL.md`：Skill 主逻辑与执行说明
- `references/url-patterns.md`：URL / raw_key 识别规则
- `scripts/xhs_download_images.py`：原图探测与下载脚本

---

## 6. 使用时的判断标准

下载完成后，建议确认：

- 文件真实存在
- 文件类型为 `image/*`
- 分辨率不是缩略图尺寸
- 链接不包含以下预览特征：
  - `sns-webpic`
  - `imageView2`
  - `!nd_prv`
  - `!nd_dft`
  - `WB_PRV`
  - `WB_DFT`

如果包含这些特征，通常不是理想原图。

---

## 7. 适用范围

适合：

- 小红书分享链接原图提取
- 无水印优先下载
- 返回更清晰的图片文件

不适合：

- 视频抓取
- 高强度批量爬取
- 绕过平台风控的自动化抓取

---

## 8. 一句话说明

这是一个专门给 OpenClaw 用的 skill，用来把小红书笔记里的图片尽量还原成原图并下载下来。

---

## 9. 免责声明

本技能是为 OpenClaw 平台编写的接口级工具。请仅在你具备相关内容版权或授权、并遵守小红书服务条款的前提下运行；避免用于侵权、规避平台策略或批量违规抓取。作者不对任何第三方因使用本技能造成的版权争议或平台限制负责。

使用建议：

- 仅在合规、自用、学习或经明确授权的场景下调用。
- 任何商业化分发、公共传播或批量提取前先获得目标平台或内容方批准。
- 如果遇到封禁/投诉，先停止自动化，再重新评估策略。

## 时间戳

- manifest.json 包含 `publish_time`（Unix 时间戳，兼容秒/毫秒）和 `publish_time_str`（"2026:10:02 18:26:12"，东八区，exiftool 直接可用）
- 下载完成后自动写入文件，无需手动跑 exiftool
