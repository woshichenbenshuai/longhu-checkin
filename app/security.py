"""Browser request protection and explicit notification destination policy."""

import os
from urllib.parse import urlsplit

DEFAULT_WEBHOOK_HOSTS = "api.day.app,qyapi.weixin.qq.com,oapi.dingtalk.com"


def validate_webhook_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        allowed = {
            host.strip().lower()
            for host in os.environ.get("WEBHOOK_ALLOWED_HOSTS", DEFAULT_WEBHOOK_HOSTS).split(",")
            if host.strip()
        }
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in allowed
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
            and not any(ord(char) <= 32 for char in url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("推送地址必须使用 HTTPS 且域名位于服务器允许列表中")
    return url


def cross_origin_write(headers) -> bool:
    if headers.get("sec-fetch-site") == "cross-site":
        return True
    origin = headers.get("origin")
    if not origin:
        return False
    try:
        parsed = urlsplit(origin)
        return parsed.scheme not in ("http", "https") or parsed.netloc != headers.get("host")
    except ValueError:
        return True
