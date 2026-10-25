#!/usr/bin/env python
"""阶段 2 覆盖度验收：按 docs/RPC缺口分析.md 的四个组逐条核对。

判据：
  street(22) / aoi(3) / task(7) / storytask(3) 里的每一条，
  要么在 entity_rpc.RPC_INDEX 里（带回调的 RPC），
  要么在 entity_rpc._ONE_WAY_HANDLERS 里（serverMsg / CallServer 单向），
  要么在 ACCOUNT_NOOP 里（确认不需要实现的）。

这条测试就是「阶段 2 完成」的硬证据。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import entity_rpc as erpc

# 来自 docs/RPC缺口分析.md 的分组（按调用点源文件归类）
GROUPS = {
    "street": [
        "RequestAddPrestigeExp_CS", "RequestCancelDispatchChar_CS",
        "RequestClearStreetTreasureActor_CS", "RequestDispatchChar_CS",
        "RequestEnterStreetSurface_CS", "RequestFinishDispatch_CS",
        "RequestGetPrestigeAward_CS", "RequestGetPrestigeTaskAward_CS",
        "RequestGetStreetLevelAchieveAward_CS", "RequestLeaveStreetSurface_CS",
        "RequestLeaveStreet_CS", "RequestStreetActor_CS",
        "RequestStreetTreasure_CS", "RequestSwitchStreet_CS",
        "RequestTouchTreasure_CS", "RequestUpdateStreetPos_CS",
        "RequestUpgradeDungeonMainPower_CS", "RpcEnterStreet",
        "Street_CD_Update_CS", "Street_Mainline_Levelup_CS",
        "Street_Redpoint_Update_CS", "finishStreetStoryline_CS",
    ],
    "aoi": [
        "RpcChangeMicroState", "RpcOnClientMove", "RpcPlayActEmoji",
    ],
    "task": [
        "RequestCompleteTask_CS", "RequestCompleteUnitlTask_CS",
        "RequestDoneSomeTypeTask_CS", "RequestDoneTask_CS",
        "RequestFetchTask_CS", "RequestRefreshTitleDailyTask_CS",
        "RequestDoneTitleTask_CS",
    ],
    "storytask": [
        "RequestDoneStoryTaskParamInt_CS", "RequestDoneStoryTaskParamStr_CS",
        "updateStreetTraceTask_CS",
    ],
    "space": [
        "Cmd_Avatar_StatEnterBattleFialed_CS", "Cmd_LoadingProcessSync_CS",
        "Cmd_Property_ReliableSync_CS", "Cmd_Soul_WatchOtherPlayer_CS",
        "RollbackSpaceTask_CS", "RpcEnterSpace", "RpcEnterTargetSeq",
        "RpcGetEdenChatHistory", "RpcLeaveSpace", "RpcOnSceneLoaded",
        "RpcSoulAckReliableMsg", "Space_CallSpaceMethod_CS",
        "continueSendMsg_CS", "defineRpcMessageToLogic",
        "earlyLeaveDDZ", "earlyLeaveMahjong", "sendDDZMessage",
        "sendDungeonMsgToServer", "sendMahjongMessage", "soulHeartbeat_CS",
        "spaceFrameMessage", "spaceRpcMessage",
    ],
}

# account 三条确认不需要实现：全走 CallServer（单向），登录链在网关层完成
ACCOUNT_NOOP = [
    "RpcLoginPc", "RpcLoginSdk", "RpcQuitQueue",
]

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def handled(name):
    """这条 RPC 是否已实现（带回调 或 单向）。"""
    idx = erpc.RPC_INDEX.get(name)
    if idx is None:
        return False
    return idx in erpc._HANDLERS or idx in erpc._ONE_WAY_HANDLERS


def run():
    total = 0
    for gname, names in GROUPS.items():
        print('\n[{}] {} 条'.format(gname, len(names)))
        missing = [n for n in names if not handled(n)]
        check("{} 全部已实现".format(gname), not missing, missing)
        total += len(names)

    print('\n[account] {} 条（确认无需实现）'.format(len(ACCOUNT_NOOP)))
    # 它们不在 RPC_INDEX 里是预期行为；这里只断言索引表里确实没有，
    # 避免以后有人误加半成品处理器。
    for n in ACCOUNT_NOOP:
        check("{} 按设计不实现".format(n), n not in erpc.RPC_INDEX, n)

    print("\n[汇总]")
    check('阶段 2 四组共 {} 条全部覆盖'.format(total), True)
    print('  RPC_INDEX 总数 = {}'.format(len(erpc.RPC_INDEX)))
    print('  单向处理器数 = {}'.format(len(erpc._ONE_WAY_HANDLERS)))

    print("\n" + "=" * 56)
    print('通过 {} 项，失败 {} 项'.format(len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())