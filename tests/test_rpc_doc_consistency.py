#!/usr/bin/env python
"""RPC 文档数字守卫：docs/RPC缺口分析.md 与 docs/RPC总表.md 的口径必须与代码一致。

★ 为什么有这个测试（反补丁驱动 R5 / P07 手写数字）

  以前「我们已实现 N 条」是从 entity_rpc.RPC_INDEX（**登记表**）数出来的，
  而真正有没有人处理是 dispatch.HANDLERS。两者差 17 条，
  于是文档长期多报「已实现 46 条」（真值是 106 条有处理器）。
  这个坑修了不止一次：换个地方统计又漂回去。

  守卫方式：文档里的数字只能由工具生成，工具的口径被本测试钉住 ——
  谁把口径改回 RPC_INDEX，这条测试立刻红。

不变量：docs/RPC缺口分析.md 里「我们已实现」的数字 ==
        len(dispatch.HANDLERS ∪ dispatch.ONE_WAY_HANDLERS) 里能在 C2S 名单中找到的条数。

用法: python tests/test_rpc_doc_consistency.py
"""
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    # 中文输出必须显式 UTF-8，否则 Windows 控制台按 GBK 编码，
    # run_all_tests 的「通过 N 项」汇总正则匹配不到（会被记成 fail=-1）。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from game_server import dispatch, rpc_catalog, rpc_index  # noqa: E402

DOC_GAP = os.path.join(ROOT, "docs", "RPC缺口分析.md")
DOC_LIST = os.path.join(ROOT, "docs", "RPC总表.md")
TSV = os.path.join(ROOT, "work", "rpc_list.tsv")

_ok = 0
_bad = []


def check(name, cond, detail=""):
    global _ok
    if cond:
        _ok += 1
        print(f"  PASS  {name}")
    else:
        _bad.append(name)
        print(f"  FAIL  {name}" + (f" | {detail}" if detail else ""))


def handled_names():
    out = set()
    for key in list(dispatch.HANDLERS) + list(dispatch.ONE_WAY_HANDLERS):
        name = rpc_catalog.name_of(key) if isinstance(key, int) else key
        if name:
            out.add(name)
    return out


def c2s_names():
    names = set()
    with open(TSV, encoding="utf-8", errors="replace") as f:
        next(f, None)
        for line in f:
            cols = line.rstrip("\r\n").split("\t")
            if len(cols) >= 3 and cols[0] == "C2S" and cols[1]:
                names.add(cols[1])
    return names


def main():
    if not os.path.exists(TSV):
        print("缺少 work/rpc_list.tsv（先跑 python tools/gen_rpc_list.py）")
        return 1
    handled = handled_names()
    c2s = c2s_names()
    expect_impl = len([n for n in c2s if n in handled])

    with open(DOC_GAP, encoding="utf-8", errors="replace") as f:
        gap = f.read()
    m = re.search(r"\|\s*我们已实现[^|]*\|\s*(\d+)\s*\|", gap)
    check("缺口分析里有「我们已实现」数字", m is not None)
    if m:
        doc_n = int(m.group(1))
        check("缺口分析的「我们已实现」== 有处理器的 C2S 条数",
              doc_n == expect_impl, f"doc={doc_n} code={expect_impl}")

    # 口径守卫：RPC_INDEX（登记表）比 Handlers 多，两者不许再混用
    registered = set(rpc_index.RPC_INDEX)
    check("登记表确实比处理器多（口径差存在，测试有意义）",
          len(registered - handled) > 0,
          f"registered={len(registered)} handled={len(handled)}")

    # 总表里也要有口径说明
    with open(DOC_LIST, encoding="utf-8", errors="replace") as f:
        lst = f.read()
    check("总表写明「有处理器（已实现）」口径", "有处理器" in lst)
    m2 = re.search(r"有处理器的（已实现）:\s*(\d+)", lst)
    check("总表命令行口径与代码一致",
          m2 is None or int(m2.group(1)) == len(handled),
          f"doc={m2.group(1) if m2 else 'missing'} code={len(handled)}")

    print()
    print(f"=== 结果: PASS={_ok} FAIL={len(_bad)} ===")
    if _bad:
        print("失败项：" + "、".join(_bad))
        return 1
    print(f"通过 {_ok} 项，失败 0 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
