"""各平台统一请求头：浏览器 UA + Referer（过基础 WAF）。

所有下载脚本统一从这里取 headers，不再各写各的。
"""
# -*- coding: utf-8 -*-

DOUYIN = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/16.0 Mobile/15E148 Safari/604.1"
    ),
    "Referer": "https://www.douyin.com/",
}

# 微博 API（ajax/statuses/show）用 PC UA + XHR 头，反爬较松
WEIBO_API = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://weibo.com/",
    "X-Requested-With": "XMLHttpRequest",
}

# 微博文件下载用 iPhone UA
WEIBO_DL = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Referer": "https://weibo.com/",
}

XIAOHONGSHU = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15"
    ),
    "Referer": "https://www.xiaohongshu.com/",
}
