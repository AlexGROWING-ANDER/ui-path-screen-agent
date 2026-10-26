"""Explicit communication consent; payment/account destruction stays separate."""
import re

PROTECTED = re.compile(r'支付|付款|转账|下单|提交订单|确认购买|立即购买|确认收货|注销|delete account|\b(?:pay|payment|purchase|checkout|transfer money)\b',re.I)
COMMUNICATION = re.compile(r'发送|发布|分享|评论|点赞|关注|发邮件|\b(?:send|publish|share|post|comment|like|follow)\b',re.I)


def authorized_communication(description: str) -> bool:
    text=str(description or '')
    return not PROTECTED.search(text) and bool(COMMUNICATION.search(text))
