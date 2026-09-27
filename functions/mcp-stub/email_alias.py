"""
工程師測試用的 email domain alias：token 裡的 email 是 A domain（例如
`@nextlink.com.tw`），但客戶環境的 Cosmos DB 會員資料（credentials）登記的是
同一個人的 B domain email（例如 `@microfusion.cloud`），兩者 @ 前面的
user name 相同——查會員時視為同一個人。

為什麼不放在 oauth_proxy.py：OAuth proxy 是無狀態 passthrough，只轉送
Entra 的 /authorize 與 /token，從頭到尾看不到、也改不了使用者身分（token
是 Entra 簽發的，proxy 無法改寫其中的 email claim）。「登入的人是誰」是在
mcp_stub 解出 token 之後、查會員資料的這一步才知道，所以 alias 放在這一層。

只適用單一組 source → target domain，由環境變數控制，兩個都沒設定（預設）
就完全停用，不影響一般客戶帳號：
- MEMBER_EMAIL_ALIAS_SOURCE_DOMAIN：例如 nextlink.com.tw
- MEMBER_EMAIL_ALIAS_TARGET_DOMAIN：例如 microfusion.cloud

只在「用原本 email 查不到會員」時才嘗試 alias（原 email 有登記就直接用原
email，不會被 alias 蓋掉）。

安全性提醒：這是靠 email claim 的 @ 前面字串做身分對應，等於信任「該 domain
的帳號是我們自己人、且 user name 跟另一個 domain 的同一位一致」。source
domain 必須完全相等（不接受子網域、不接受後綴相似的網域），且僅供測試環境／
工程師使用，正式客戶環境不應該啟用。
"""

import logging
import os

from member_repository import MemberInfo, MemberRepository


def alias_email(email: str) -> str | None:
    """回傳 alias 後的 email；未啟用、或 email 不屬於 source domain 時回傳 None。"""
    source_domain = os.environ.get("MEMBER_EMAIL_ALIAS_SOURCE_DOMAIN", "").strip().lower().lstrip("@")
    target_domain = os.environ.get("MEMBER_EMAIL_ALIAS_TARGET_DOMAIN", "").strip().lower().lstrip("@")
    if not source_domain or not target_domain:
        return None

    user_name, at, domain = email.strip().rpartition("@")
    if not at or not user_name or domain.lower() != source_domain:
        return None

    return f"{user_name.lower()}@{target_domain}"


def get_member_with_alias(repo: MemberRepository, email: str) -> MemberInfo | None:
    """先用原 email 查會員；查不到（None）才嘗試 alias email。"""
    member = repo.get_member(email)
    if member is not None:
        return member

    alias = alias_email(email)
    if alias is None:
        return None

    member = repo.get_member(alias)
    logging.info(
        "member lookup: email=%s not found, tried alias email=%s found=%s",
        email,
        alias,
        member is not None,
    )
    return member
