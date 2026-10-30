import os
from pathlib import Path

root = Path(__file__).resolve().parents[1]
os.chdir(root)
for line in (root / ".private/local.env").read_text().splitlines():
    if line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ[k] = v

from bybit_mcp.app import main
main()
