"""打印 CFFXDJ_5_V3 predictions + strategy_defs#1 的哈希（用于确认 V3 未被改动）。"""
import hashlib, sqlite3, sys
from pathlib import Path
db = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "data" / "app.db")
c = sqlite3.connect(db)
a = list(c.execute("select * from predictions where strategy='CFFXDJ_5_V3' order by id"))
d = list(c.execute("select * from strategy_defs where id=1"))
print(hashlib.sha256(repr((a, d)).encode()).hexdigest()[:16])
