"""背包相关「暂时做不到」的落盘清单。

两类东西要被看见、而不是静默吞掉：

1. **效果未实现**：useType 既没 handler 也没在 UNSUPPORTED 里（或属于 PENDING）
2. **道具 id 不在 ItemData**：宝箱/结算配置里引用了装备类 id（4205 在 EquipmentData 里），
   背包无法凭空造道具 —— 记下来，等对应系统上线

落盘格式（UTF-8 TSV，只有一份表头）：

    kind<TAB>key<TAB>reason<TAB>first_seen

· 同 (kind, key) **跨进程**只记一次：启动时扫描已有文件把键读进内存，
  这样重启服务不会把同一批"做不到"重复刷进文件。
· 兼容历史格式（早期首版写的是 item_id/use_type），读到旧行时按其前两列当 (kind,key)，
  **不重写**旧内容（保留审计痕迹）。
"""
import os
from time import time

from shared.log_tee import get_logger

_log = get_logger(__name__)

HEADER = "kind\tkey\treason\tfirst_seen\n"
LEGACY_HEADERS = {"item_id\tuse_type\treason\tfirst_seen", "kind\tkey\treason\tfirst_seen"}

_PATH: str | None = None
_SEEN: set[tuple[str, str]] = set()


def _path() -> str:
    global _PATH
    if _PATH is None:
        from ... import config as _cfg

        d = getattr(_cfg, "LOG_DIR", "work/logs")
        os.makedirs(d, exist_ok=True)
        _PATH = os.path.join(d, "unknown_item_effects.tsv")
    return _PATH


def _seed() -> None:
    """把已有文件里的 (kind,key) 读进内存，做跨进程去重。"""
    if _SEEN:
        return
    p = _path()
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 2 or line.rstrip("\n") in LEGACY_HEADERS:
                    continue
                _SEEN.add((parts[0], parts[1]))
    except FileNotFoundError:
        return
    except OSError as e:                     # 读不了就当空文件，不影响业务
        _log.warning("[bag] 读取 unknown_item_effects.tsv 失败：%s", e)


def note(kind: str, key: object, reason: str) -> None:
    """记一条（kind, key）只记一次。落盘失败不影响业务。"""
    sig = (str(kind), str(key))
    if sig in _SEEN:
        return
    _seed()
    if sig in _SEEN:
        return
    _SEEN.add(sig)
    try:
        p = _path()
        new = not os.path.exists(p) or os.path.getsize(p) == 0
        with open(p, "a", encoding="utf-8") as f:
            if new:
                f.write(HEADER)
            f.write(f"{kind}\t{key}\t{reason}\t{int(time())}\n")
    except OSError as e:
        _log.warning("[bag] 落盘 unknown_item_effects.tsv 失败：%s", e)


def reset_for_tests() -> None:
    """测试用：忘记缓存（文件不动）。"""
    global _PATH
    _PATH = None
    _SEEN.clear()
