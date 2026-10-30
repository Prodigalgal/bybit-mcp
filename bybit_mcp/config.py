import os
from dataclasses import dataclass
from pathlib import Path

SCOPES = {
    "bybit:read": "读取行情、账户、订单和仓位",
    "bybit:trade": "下单、改单、撤单、杠杆和止盈止损",
    "bybit:account": "修改账户、保证金和产品设置",
    "bybit:funds": "资金划转、借贷、提现及模拟资金申请",
    "bybit:credentials": "管理 Bybit API 密钥和子账户",
}


@dataclass
class Settings:
    public_url: str
    admin_token: str
    encryption_key: str
    data_dir: Path = Path("data")
    full_tools: bool = False
    proxy: str | None = None

    @classmethod
    def from_env(cls):
        s = cls(
            public_url=os.environ.get("PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/"),
            admin_token=os.environ.get("ADMIN_TOKEN", ""),
            encryption_key=os.environ.get("ENCRYPTION_KEY", ""),
            data_dir=Path(os.environ.get("DATA_DIR", "data")),
            full_tools=os.environ.get("FULL_ENDPOINT_TOOLS", "false").lower() == "true",
            proxy=os.environ.get("BYBIT_HTTP_PROXY") or None,
        )
        if len(s.admin_token) < 32 or not s.encryption_key:
            raise RuntimeError("ADMIN_TOKEN (32+ characters) and ENCRYPTION_KEY are required")
        return s

    @property
    def resource(self):
        return self.public_url + "/mcp"
