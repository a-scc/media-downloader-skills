#!/usr/bin/env python3
"""
微博媒体下载器
纯 Python 实现，一键下载微博图片+视频。

用法: python3 wb.py <微博链接> [保存目录]
     python3 wb.py --batch links.txt [保存目录]

支持链接格式:
  • 标准微博:   https://weibo.com/USER/STATUS_ID
  • 分享链接:   https://mapp.api.weibo.cn/fx/XXXX.html
  • 移动端:     https://m.weibo.cn/status/STATUS_ID
"""

import sys
import os

from wb_core import WeiboDownloader


def download_weibo(url, output_dir=None):
    """下载单条微博媒体"""
    dl = WeiboDownloader()
    result = dl.download(url, output_dir)
    return result.get("success", False), result.get("files", 0), result.get("dir", output_dir or os.getcwd())


def download_batch(batch_file, output_dir=None):
    """批量下载"""
    if not os.path.exists(batch_file):
        print(f"❌ 批处理文件不存在: {batch_file}")
        return False, 0

    with open(batch_file, 'r', encoding='utf-8') as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]

    print(f"📋 批量下载: {len(urls)} 个链接\n")

    total_files = 0
    success = 0
    fail = 0

    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] ", end='')
        ok, count, _ = download_weibo(url, output_dir)
        if ok:
            success += 1
            total_files += count
        else:
            fail += 1
        print()

    print(f"{'═' * 50}")
    print(f"📊 批量下载统计:")
    print(f"   ✅ 成功: {success}/{len(urls)}")
    print(f"   ❌ 失败: {fail}/{len(urls)}")
    print(f"   📦 总文件数: {total_files}")
    return fail == 0, total_files


def show_help():
    print("""📱 微博媒体下载器
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
纯 Python 实现，一键下载微博图片+视频

支持链接格式:
  • 标准微博:   https://weibo.com/USER/STATUS_ID
  • 分享链接:   https://mapp.api.weibo.cn/fx/XXXX.html
  • 移动端:     https://m.weibo.cn/status/STATUS_ID

用法:
  python3 wb.py <链接> [保存目录]
  python3 wb.py --batch links.txt [保存目录]

示例:
  python3 wb.py "https://weibo.com/user/123456"
  python3 wb.py "https://mapp.api.weibo.cn/fx/xxxx.html" /tmp/weibo
  python3 wb.py --batch weibo_links.txt
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━""")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        show_help()
        return

    # 支持 --out-dir 参数（与其他两个脚本统一）
    args = sys.argv[1:]
    output_dir = None
    if "--out-dir" in args:
        idx = args.index("--out-dir")
        if idx + 1 < len(args):
            output_dir = args[idx + 1]
        args = args[:idx] + args[idx + 2:]

    if not args:
        show_help()
        return

    if args[0] == '--batch':
        batch_file = args[1] if len(args) > 1 else 'links.txt'
        # 兼容旧的位置参数
        if len(args) > 2 and output_dir is None:
            output_dir = args[2]
        download_batch(batch_file, output_dir)
    else:
        url = args[0]
        # 兼容旧的位置参数
        if len(args) > 1 and output_dir is None:
            output_dir = args[1]
        download_weibo(url, output_dir)


if __name__ == '__main__':
    main()
