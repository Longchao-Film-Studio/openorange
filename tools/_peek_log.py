# -*- coding: utf-8 -*-
import sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

LOG = r"F:/ghidra/out/analysis.log"
txt = open(LOG, encoding="utf-8", errors="replace").read()

# 找脚本输出的起点
i = txt.find("imageBase =")
print("imageBase 位置:", i)
seg = txt[i:]
print("从该处到结尾的长度:", len(seg))
print()
print("=== 前 1200 字符（看日志格式）===")
print(seg[:1200])