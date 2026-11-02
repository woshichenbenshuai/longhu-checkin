"""Telegram notifications; credentials are read from a server-only secret file."""

import json
import logging
import os
import re
from pathlib import Path

import requests

logger = logging.getLogger(__name__)
DEFAULT_CONFIG = "/run/secrets/telegram.json"


def _config() -> dict:
    path = Path(os.environ.get("TELEGRAM_CONFIG_FILE", DEFAULT_CONFIG))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Invalid config")
        token, chat_id = data.get("bot_token"), data.get("chat_id")
        if token is not None and (not isinstance(token, str) or not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token)):
            raise ValueError("Invalid token")
        if chat_id is not None and (isinstance(chat_id, bool) or not isinstance(chat_id, (str, int))):
            raise ValueError("Invalid chat")
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("Telegram 配置无法读取，推送已跳过")
        return {}


def status() -> dict:
    config = _config()
    configured = bool(config.get("bot_token") and config.get("chat_id"))
    return {"configured": configured, "enabled": configured and config.get("enabled") is True}


def redact(text: str) -> str:
    # Never send bearer tokens accidentally echoed in upstream error messages.
    text = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b", "[已隐藏凭据]", str(text))
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[已隐藏凭据]", text)
    return re.sub(r"\b[A-Za-z0-9]{32,}\b", "[已隐藏凭据]", text)


def _chunks(text: str):
    # Telegram counts UTF-16 units; keep room for non-BMP characters in names.
    chunk, units = [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > 3900:
            yield "".join(chunk)
            chunk, units = [], 0
        chunk.append(char)
        units += width
    if chunk:
        yield "".join(chunk)


def send_message(text: str) -> bool:
    config = _config()
    token, chat_id = config.get("bot_token"), config.get("chat_id")
    if config.get("enabled") is not True or not token or not chat_id or not text.strip():
        return False
    session = requests.Session()
    session.trust_env = False
    try:
        for chunk in _chunks(redact(text).replace(str(token), "[已隐藏凭据]")):
            response = session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk, "link_preview_options": {"is_disabled": True}},
                timeout=(5, 15), allow_redirects=False,
            )
            data = response.json()
            if response.status_code != 200 or not isinstance(data, dict) or data.get("ok") is not True:
                logger.warning("Telegram 推送失败：HTTP %s", response.status_code)
                return False
        return True
    except (requests.RequestException, ValueError) as exc:
        # Request exceptions may contain the bot token in the URL.
        logger.warning("Telegram 推送失败：%s", type(exc).__name__)
        return False
    finally:
        session.close()


if __name__ == "__main__":
    import sys
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if sys.argv[1:] != ["--test"]:
        raise SystemExit("Usage: python -m app.telegram --test")
    now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    ok = send_message(
        f"【龙湖签到｜推送测试】\n来源：家里 common\n时间：{now}（北京时间）\n"
        "Telegram 推送已接通。\n自动签到和手动签到结束后会发送结果，失败或续期异常也会提醒。\n"
        "这是一条测试通知，尚未执行签到。"
    )
    print("Telegram test delivered" if ok else "Telegram test failed; credentials omitted")
    raise SystemExit(0 if ok else 1)
