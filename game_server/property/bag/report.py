"""背包相关「暂时做不到」的落盘清单。

两类东西需要被看见、而不是静默吞掉：

1. **效果未实现**：useType 既没 handler 也没在 UNSUPPORTED 里（或属于 PENDING）
2. **道具 id 不在 ItemData**：宝箱/结算配置里引用了装备类 id（4205 在 EquipmentData 里），
   背包无法凭空造道具 —— 记下来，等对应系统上线

落盘格式：`work/logs/unknown_item_effects.tsv`，同 (kind, key) 只记一次，带首次时间。
"""
import os
import time

from shared.log_tee import get_logger

_log = get_logger(__name__)

_PATH = None
_SEEN: set[tuple[str, str]] = set()


def _path() -> str:
    global _PATH
    if _PATH is None:
        from ... import config as _cfg

        d = getattr(_cfg, "LOG_DIR", "work/logs")
        os.makedirs(d, exist_ok=True)
        _PATH = os.path.join(d, "unknown_item_effects.tsv")
    return _PATH


def note(kind: str, key: object, reason: str) -> None:
    """记一条（kind, key）只记一次。落盘失败不影响业务。"""
    sig = (str(kind), str(key))
    if sig in _SEEN:
        return
    _SEEN.add(sig)
    try:
        p = _path()
        new = not os.path.exists(p)
        with open(p, "a", encoding="utf-8") as f:
            if new:
                f.write("kind\tkey\treason\tfirst_seen\n")
            f.write(f"{kind}\t{key}\t{reason}\t{int(time.time())}\n")
    except OSError as e:
        _log.warning("[bag] 落盘 unknown_item_effects.tsv 失败：%s", e)


def reset_for_tests() -> None:
    global _PATH
    _PATH = None
    _SEEN.clear()
