"""背包错误码（与客户端 ErrorConst 对齐）。

客户端依据 errorcode 决定提示文案（avatarbagcomp.lua:348-369）：

    Bag_InvaildItemId     15002  道具不存在
    Bag_MaxNumExceed      15003  超过单格上限
    Bag_InvaildId         15004  格子键无效
    Bag_InvaildNum        15005  数量无效
    Bag_InvaildExtParams  15006  ext 参数非法（缺 id / 缺 itemId …）
    Bag_InvaildItemList   15007  发放列表非法（池子不存在 / 数据坏）
    Bag_LevelNotEnough    15008  等级不足
    Bag_SystemNotUnlocked 15009  系统未开放（本方案用它标志「效果已登记为暂不支持」）
    Bag_NotInBPSeason     15010  不在通行证赛季
"""

BAG_OK = 15001
BAG_INVALID_ITEM_ID = 15002
BAG_MAX_NUM_EXCEED = 15003
BAG_INVALID_ID = 15004
BAG_INVALID_NUM = 15005
BAG_INVALID_EXT_PARAMS = 15006
BAG_INVALID_ITEM_LIST = 15007
BAG_LEVEL_NOT_ENOUGH = 15008
BAG_SYSTEM_NOT_UNLOCKED = 15009
BAG_NOT_IN_BP_SEASON = 15010


class BagError(Exception):
    """可预期的背包错误：带错误码，调用方转成客户端响应。

    ★ 用它而不是返回值里的魔法字符串：错误码是客户端契约的一部分，
      漏掉 / 写错会变成「点了没反应」。
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"
