"""任务编排：续期 → 签到 → 状态刷新 → 落库 → 告警。"""

import logging
import math
from datetime import datetime, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from . import db, longhu, security, telegram

logger = logging.getLogger(__name__)

BARK_DEFAULT_HOSTS = ("day.app",)
BARK_TITLE = "龙湖签到"
BOT_HOSTS = ("qyapi.weixin.qq.com", "oapi.dingtalk.com")


def days_left(expire_at: str | None) -> int | None:
    if not expire_at:
        return None
    try:
        dt = datetime.fromisoformat(expire_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = dt - datetime.now(timezone.utc)
        # 向上取整：刚续期完应显示 30 天，而不是 29 天
        return max(0, math.ceil(delta.total_seconds() / 86400))
    except Exception:
        return None


def _is_bark(url: str) -> bool:
    """判断是不是 Bark 地址。

    支持官方 https://api.day.app/<key> 和自建服务 https://bark.example.com/<key>。
    自建服务靠「路径只有一段、且不是群机器人域名」来判断。
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in BARK_DEFAULT_HOSTS):
        return True
    if any(bot in host for bot in BOT_HOSTS):
        return False
    segments = [s for s in parsed.path.split("/") if s]
    return len(segments) == 1 and not parsed.query


def _build_notify(url: str, text: str) -> tuple[str, dict]:
    """按 webhook 类型返回 (请求地址, 请求体)。

    - Bark：POST <origin>/push，body 里带 device_key
    - 企业微信 / 钉钉群机器人：{"msgtype": "text", ...}
    """
    if _is_bark(url):
        parsed = urlparse(url)
        key = [s for s in parsed.path.split("/") if s][0]
        scheme = parsed.scheme or "https"
        return f"{scheme}://{parsed.netloc}/push", {
            "device_key": key,
            "title": BARK_TITLE,
            "body": text,
            "group": BARK_TITLE,
        }
    return url, {"msgtype": "text", "text": {"content": text}}


def notify(text: str):
    """向已配置的 Telegram 和可选 Webhook 发送通知。"""
    text = telegram.redact(text)
    telegram.send_message(text)
    _notify_webhook(text)


def _notify_webhook(text: str):
    url = (db.get_settings().get("webhook_url") or "").strip()
    if not url:
        return

    try:
        url = security.validate_webhook_url(url)
    except ValueError:
        logger.warning("推送已跳过：地址不在服务器允许列表中")
        return

    endpoint, payload = _build_notify(url, text)
    session = requests.Session()
    session.trust_env = False  # 忽略系统代理，避免被本机抓包工具拦住
    try:
        resp = session.post(endpoint, json=payload, timeout=10, allow_redirects=False)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        # Bark 用 code（成功为 200），企业微信/钉钉用 errcode（成功为 0）
        code = data.get("code", data.get("errcode", 0))
        if resp.status_code >= 300 or code not in (0, 200):
            logger.warning("webhook 推送返回异常: HTTP %s", resp.status_code)
    except Exception as exc:  # 告警失败不影响主流程
        logger.warning("webhook 推送失败: %s", type(exc).__name__)
    finally:
        session.close()


def run_account(account: dict, do_checkin: bool = True) -> dict:
    """执行单个账号的完整流程，返回可直接给前端用的结果。"""
    acc_id = account["id"]
    acc_name = account["name"]
    result = {
        "account_id": acc_id,
        "name": acc_name,
        "refreshed": False,
        "checkin_status": "skipped",
        "checkin_message": "",
        "profile": {},
        "error": "",
        "growth_before": account.get("growth_value", 0),
    }

    # ---- 1. 续期 ----
    try:
        ok, payload = longhu.refresh_token(account)
    except longhu.LonghuError as exc:
        ok, payload = False, str(exc)

    if ok:
        result["refreshed"] = True
        if payload != account.get("token"):
            db.update_account(acc_id, token=payload)
            account["token"] = payload
        db.update_account(acc_id, expire_at=longhu.expire_at())
        db.add_log(acc_id, acc_name, "refresh", "ok", "token 有效期已续至 30 天")
    else:
        db.add_log(acc_id, acc_name, "refresh", "fail", payload)
        result["error"] = payload
        logger.warning("[%s] 续期失败: %s", acc_name, payload)

    # ---- 2. 签到 ----
    if do_checkin:
        try:
            status, message = longhu.checkin(account)
        except longhu.LonghuError as exc:
            status, message = "fail", str(exc)
        result["checkin_status"] = status
        result["checkin_message"] = message
        db.add_log(acc_id, acc_name, "checkin", status, message)
        db.update_account(acc_id, last_status=status, last_message=message)

    # ---- 3. 刷新资料 ----
    try:
        profile = longhu.fetch_profile(account)
        result["profile"] = profile
        db.update_account(
            acc_id,
            growth_value=profile.get("growth_value", 0),
            level_name=profile.get("level_name", ""),
            balance=profile.get("balance", 0),
            nickname=profile.get("nickname", ""),
        )
    except longhu.LonghuError as exc:
        result["error"] = result["error"] or str(exc)
        db.add_log(acc_id, acc_name, "query", "fail", str(exc))

    db.update_account(acc_id, last_run_at=db.now_iso())

    days = days_left(db.get_account(acc_id).get("expire_at"))
    result["days_left"] = days
    return result


def run_all(do_checkin: bool = True) -> list[dict]:
    accounts = db.list_accounts(only_enabled=True)
    results = []
    for account in accounts:
        try:
            results.append(run_account(account, do_checkin=do_checkin))
        except Exception as exc:  # 单个账号异常不影响其他账号
            logger.exception("[%s] 执行异常", account.get("name"))
            db.add_log(account["id"], account["name"], "run", "fail", f"异常: {exc}")
            results.append({
                "account_id": account["id"], "name": account["name"],
                "checkin_status": "fail", "checkin_message": str(exc), "error": str(exc),
            })
    return results


def daily_summary(results: list[dict], source: str = "每日自动"):
    """每轮只发一次完整结果，已签、续期或资料异常同样会通知。"""
    if not results:
        return
    now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    ok = sum(r.get("checkin_status") == "ok" for r in results)
    already = sum(r.get("checkin_status") == "already" for r in results)
    failed = sum(r.get("checkin_status") == "fail" for r in results)
    issues = sum(bool(r.get("error")) or r.get("checkin_status") == "fail" for r in results)
    lines = [
        f"【龙湖签到｜{source}】", "来源：家里 common",
        f"时间：{now}（北京时间）",
        f"签到：成功 {ok}｜已签 {already}｜失败 {failed}",
        f"异常账号：{issues}",
    ]
    labels = {"ok": "签到成功", "already": "今日已签到", "fail": "签到失败", "skipped": "未执行签到"}
    for result in results:
        profile = result.get("profile") or {}
        lines.extend(["", f"账号：{str(result.get('name') or '龙湖账号')[:80]}",
                      f"状态：{labels.get(result.get('checkin_status'), '执行异常')}"])
        if result.get("checkin_message"):
            lines.append(str(result["checkin_message"])[:250])
        if "growth_value" in profile:
            current = profile["growth_value"]
            before = result.get("growth_before")
            if before is not None:
                lines.append(f"成长值：{before} → {current}（{current - before:+g}）")
            else:
                lines.append(f"成长值：{current}")
        if profile.get("level_name"):
            lines.append(f"等级：{profile['level_name']}")
        if "balance" in profile:
            lines.append(f"珑珠余额：{profile['balance']:g}")
        lines.append("凭据续期：" + ("成功" if result.get("refreshed") else "失败"))
        if result.get("days_left") is not None:
            lines.append(f"凭据剩余：{result['days_left']} 天")
        if result.get("error"):
            lines.append("异常：" + str(result["error"])[:250])
    notify("\n".join(lines))
