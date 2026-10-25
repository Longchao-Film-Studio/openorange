#!/usr/bin/env python3
"""0-4 阶段登录链路的三条查询应答（P0-E2）。

这三条都是「客户端登录后问一句、服务端照实答」，没有状态要改：
  RpcGetSunActLevel_CS  -> sun_daily_level_ids（没有周常数据就给空表）
  RpcGetMatchDuration   -> predictMatchTime
  QueryMyTeamInfo_CS    -> 该玩家不在任何队伍里（errcode=-1 客户端安静返回）
"""
from .. import ms as msmod


def _sunact_level(rid, _params, _uid=None):
    """RpcGetSunActLevel_CS -> RpcGetSunActLevel_SC。

    avatarsunactcomp.lua:315-321：
      RpcGetSunActLevel_SC(ret, response) -> self.sun_daily_level_ids = response.levelIds
    没有周常数据就给空表，客户端 response.levelIds 拿到 {}，索引得到 nil，
    avatarsunactcomp.lua:226 的 if not self.sun_daily_level_ids 也放行。
    """
    return msmod.build_sunact_level_sc(rid)

def _match_duration(rid, _params, _uid=None):
    """RpcGetMatchDuration -> 应答。

    avatarmatch.lua:73-79：
      self:serverRpc("RpcGetMatchDuration", {}, function(retstatus, response)
        if retstatus.status then self.predictMatchTime = response.data or {} end
      end)
    """
    return msmod.build_match_duration_sc(rid)

def _team_info(rid, _params, _uid=None):
    """QueryMyTeamInfo_CS -> 应答（该玩家不在任何队伍里）。

    avatarteamcomp.lua:688-707：
      QueryMyTeamInfo_SC(cb, retstatus, response)：
        status 为假 -> self:ResetTeamInfo()，cb(false)
      CheckTeamStatus -> TeamErrorResult(nil, nil)：errcode=-1 查不到
      TeamErrorOrient，安静返回，不会弹错误提示。
    """
    return msmod.build_team_info_sc(rid)
