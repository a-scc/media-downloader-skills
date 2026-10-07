"""可配置限流间隔：不同时间段平台限流强度不一样，调参数比改代码快。"""
# -*- coding: utf-8 -*-

# 图片下载间隔（秒）：连续请求间隔，防止 CDN 限流降级 / WAF
RATE_LIMITS = {
    "xiaohongshu_image": 3,  # 小红书：3 秒，否则 CDN 降级返回 PNG/JPEG（用户 2026-10-07 实测）
    "douyin_image": 2,       # 抖音：2 秒，防 WAF
    "weibo_image": 2,        # 微博：2 秒
    # 视频都是单文件下载，无需间隔
}

# 429/403 指数退避重试次数
MAX_RETRIES = 3
