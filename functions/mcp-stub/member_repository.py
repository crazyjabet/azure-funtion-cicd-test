"""
會員資料查詢介面。

2026-09-11 更新：Cosmos DB schema 已由 SA 提供（見 CosmosDB_Table_Schema.md），
container 名稱、欄位都已確定，不再是 TODO_DATABASE_NAME 那種佔位字串了
（Terraform 已對應更新：module.functions 的 cosmos_credentials_container_name /
cosmos_companies_container_name）。

2026-09-12 更新：get_member() 的 join 查詢邏輯（依 email 查 credentials 拿
business_number，再依 business_number 查 companies 拿 tier）已實作——quota
機制（quota_service.py）需要 business_number 才能查/寫 quota_usage，這輪
一併補上，不再是 NotImplementedError。

切換方式：環境變數 MEMBER_REPOSITORY_MODE = "mock" | "cosmos"（預設 mock）。
"""

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class MemberInfo:
    email: str
    tier: str            # 例如 "free" / "paid"，實際列舉值待貿協會員分級規則確認
    is_active: bool
    source: str           # "mock" 或 "cosmos"，方便 log 判斷資料來源
    business_number: str | None = None  # quota_usage 的 partition key；查不到所屬公司時為 None（見 CosmosMemberRepository 說明）


class MemberRepository(ABC):
    @abstractmethod
    def get_member(self, email: str) -> MemberInfo | None:
        """依 email 查會員資料；查不到回傳 None（視為非會員）。"""


class MockMemberRepository(MemberRepository):
    """
    回傳假資料，供本機單元測試使用——**不對應任何真實帳號**。

    2026-09-13：先前這裡用使用者本人的真實 email（`jason.hsiao@nextlink.com.tw`）
    當測試 fixture，使用者明確要求「demo/測試帳號都要實際打到 Cosmos DB」、
    「現在都不要有 mock 的東西」——這代表 `jason.hsiao@nextlink.com.tw` 這個
    真實帳號的會員資料現在改為真實寫在 Cosmos DB（`docs/testing/` 底下的部署
    測試報告有記錄種資料的過程），不應該再跟一個寫死在程式碼裡的假資料綁在
    一起，即使該假資料現在已經不會在 `taitra-mcp-test` 環境被用到
    （`MEMBER_REPOSITORY_MODE` 已改設為 `cosmos`，見 functions.tf）。改用一個
    明顯不是任何人真實帳號的合成 email 當測試 fixture，純粹只為了本機
    `pytest`（`MEMBER_REPOSITORY_MODE` 預設值、離線測試不需要真的接 Cosmos DB）
    保留「已知付費會員」這條路徑的覆蓋率。

    _FAKE_MEMBERS 先手動塞一筆測試帳號，非測試帳號一律回傳「free tier、非付費會員」，
    避免預設值誤判成付費會員。
    """

    _FAKE_MEMBERS: dict[str, MemberInfo] = {
        "known-paid-member@example.com": MemberInfo(
            email="known-paid-member@example.com",
            tier="paid",
            is_active=True,
            source="mock",
            business_number="mock-business-number",
        ),
    }

    def get_member(self, email: str) -> MemberInfo | None:
        member = self._FAKE_MEMBERS.get(email)
        if member is not None:
            return member
        # 找不到的 email：預設當作 free tier、非付費會員，不是「查詢失敗」。
        # business_number=None 是刻意的——這不是一家真的公司，quota_service
        # 拿到 None 時會跳過配額檢查並記警告 log（見 function_app.py）。
        return MemberInfo(email=email, tier="free", is_active=True, source="mock", business_number=None)


class CosmosMemberRepository(MemberRepository):
    """
    真正的 Cosmos DB 查詢實作。

    連線用 Managed Identity（DefaultAzureCredential，見
    environments/test/cosmosdb.tf 的 azurerm_cosmosdb_sql_role_assignment）
    ——中間曾因為 terraform apply 的帳號建不了 RBAC role assignment 短暫改用
    COSMOS_DB_KEY，後來確認那是權限傳播延遲、不是真的權限不足（直接重試建立
    role assignment 就成功了），已改回 Managed Identity。

    2026-09-12：get_member() 的 join 查詢邏輯已實作——quota 機制
    （quota_service.py）需要 business_number 才能查/寫 quota_usage，這輪
    一併補上。查詢方式：
    1. 依 email 對 credentials container 做 Point Read（id = contact_email =
       partition key，見 CosmosDB_Table_Schema.md），查不到就回傳 None
       （非會員，非查詢失敗）。
    2. credentials 文件裡的 business_number 可能是 None（尚未關聯到公司，
       或該聯絡人在 HubSpot 端還沒有 Company Association）——此時無法查
       tier，比照 MockMemberRepository 對「找不到」的既有保守慣例，回傳
       tier="free"、business_number=None（quota_service 拿到 None 會跳過
       配額檢查，不是直接拒絕，因為「查不到所屬公司」跟「本來就沒有配額」是
       兩件不同的事，不應該互相冒充）。
    3. 有 business_number 才對 companies container 做 Point Read 拿 tier；
       查不到（理論上不該發生，credentials 的 business_number 是從實際存在
       的公司文件複製過去的，但資料不一致是可能的）則記警告 log 並同樣視為
       free tier，不中斷整個請求。

    is_active 對應 credentials.credential_issued（schema 文件定義：「是否
    已核准啟用 MCP 服務存取」）——這是該欄位唯一明確對應的意義，不是本輪另外
    發明的判斷邏輯。目前呼叫端（function_app.py）尚未真的依 is_active 擋
    請求，這是另一個獨立的存取控管決策，不在本輪配額任務範圍內。
    """

    def __init__(self) -> None:
        from azure.cosmos import CosmosClient
        from azure.identity import DefaultAzureCredential

        endpoint = os.environ["COSMOS_DB_ENDPOINT"]  # 例如 https://<account>.documents.azure.com:443/
        self._client = CosmosClient(endpoint, credential=DefaultAzureCredential())

        self._database_name = os.environ["COSMOS_DB_DATABASE_NAME"]
        self._credentials_container_name = os.environ["COSMOS_DB_CREDENTIALS_CONTAINER_NAME"]
        self._companies_container_name = os.environ["COSMOS_DB_COMPANIES_CONTAINER_NAME"]

    def _container(self, name: str):
        return self._client.get_database_client(self._database_name).get_container_client(name)

    def get_member(self, email: str) -> MemberInfo | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        credentials_container = self._container(self._credentials_container_name)
        try:
            credential = credentials_container.read_item(item=email, partition_key=email)
        except CosmosResourceNotFoundError:
            return None

        business_number = credential.get("business_number")
        if not business_number:
            logging.warning("member lookup: email=%s has no business_number in credentials, treating as free tier", email)
            return MemberInfo(
                email=email,
                tier="free",
                is_active=bool(credential.get("credential_issued", False)),
                source="cosmos",
                business_number=None,
            )

        companies_container = self._container(self._companies_container_name)
        try:
            company = companies_container.read_item(item=business_number, partition_key=business_number)
            tier = company.get("tier", "free")
        except CosmosResourceNotFoundError:
            logging.warning(
                "member lookup: email=%s references business_number=%s but no such company document exists, treating as free tier",
                email,
                business_number,
            )
            tier = "free"

        return MemberInfo(
            email=email,
            tier=tier,
            is_active=bool(credential.get("credential_issued", False)),
            source="cosmos",
            business_number=business_number,
        )


def get_member_repository() -> MemberRepository:
    mode = os.environ.get("MEMBER_REPOSITORY_MODE", "mock").lower()
    if mode == "cosmos":
        return CosmosMemberRepository()
    return MockMemberRepository()
