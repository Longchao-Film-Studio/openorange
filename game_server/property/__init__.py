"""game_server.property —— 属性同步地基（P0）。

模块：
  ids.py   单一 __id__ 分配器
  （后续）spec.py / tx.py / sync.py
"""

from .ids import (alloc_id, max_id, peek_next_id,
                  reconcile, verify_unique)
