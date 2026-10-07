"""指数退避重试：遇到 429/403 别直接失败，等 2^n 秒重试 3 次。

很多 WAF 拦截是临时的，退一下就过去了。
"""
# -*- coding: utf-8 -*-

import functools
import time


class RetryableHTTPError(Exception):
    """可重试的 HTTP 错误（429/403）。"""

    def __init__(self, status_code, message=""):
        self.status_code = status_code
        super().__init__(message or f"HTTP {status_code}")


def _status_of(exc):
    """从异常中提取 HTTP 状态码（支持 requests.HTTPError 和 RetryableHTTPError）。"""
    if isinstance(exc, RetryableHTTPError):
        return exc.status_code
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None)


def check_response(resp):
    """检查 requests 响应，429/403 转为可重试异常，其余 4xx/5xx 直接抛。"""
    if resp.status_code in (429, 403):
        raise RetryableHTTPError(resp.status_code, f"HTTP {resp.status_code}: {resp.url[:80]}")
    resp.raise_for_status()
    return resp


def with_retry(max_retries=3, backoff_base=2):
    """装饰器：429/403 时等待 backoff_base^n 秒后重试，最多 max_retries 次。

    用法:
        @with_retry()
        def fetch(...):
            r = requests.get(url, ...)
            return check_response(r)
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for n in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    status = _status_of(e)
                    if status in (429, 403) and n < max_retries - 1:
                        wait = backoff_base ** n  # 1s, 2s, 4s
                        print(f"[~] {status} 被限流，{wait}s 后重试 ({n + 1}/{max_retries})...")
                        time.sleep(wait)
                        last_exc = e
                        continue
                    raise
            raise last_exc
        return wrapper
    return decorator
