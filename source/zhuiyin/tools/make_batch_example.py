"""Build public editable batch examples; preview parsing only, never execute tasks."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_suite import prepare_uploads

DEST = ROOT / "data" / "deliveries" / "批量测试制作模板-20261020"
archive_path = DEST.parent / (DEST.name + ".zip")
if DEST.exists() or archive_path.exists():
    raise FileExistsError("模板已存在；请保留已编辑的文件，不覆盖。需要重建时请改用新目录名称。")
DEST.mkdir(parents=True)


def write_csv(filename, rows):
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    (DEST / filename).write_bytes(output.getvalue().encode("utf-8-sig"))


write_csv("设备场景.csv", [
    ["设备名称", "库存", "借用数量", "请求编号", "种子", "故障"],
    ["练习用投影仪", 3, 1, "practice-equipment-001", 42, ""],
    ["练习用录音笔", 5, 2, "practice-equipment-002", 17, "提交后响应丢失；读取过期状态"],
    ["练习用相机", 2, 1, "practice-equipment-003", 7, "业务内容损坏"],
])
write_csv("工单场景.csv", [
    ["业务场景", "工单标题", "优先级", "请求编号", "种子", "故障"],
    ["ticket", "练习：会议室投影无法开机", "normal", "practice-ticket-001", 42, ""],
    ["ticket", "练习：实验室网络中断", "high", "practice-ticket-002", 17, "未提交却返回成功"],
])

instructions = """# 批量测试包制作说明

这是公开的操作练习模板，共5个场景。已经用于导入预览检查，不是盲测包，也不保证某种策略必胜。未运行规则实验或真实模型。

## 最简单的做法

1. 解压模板，用 Excel 或 WPS 打开“设备场景.csv”或“工单场景.csv”。
2. 保留第一行表头，每行写一个任务，修改业务值、故障和请求编号。
3. 另存为“CSV UTF-8（逗号分隔）”，不要保存成 XLSX 后只改扩展名。
4. 单个 CSV 可以直接上传；多个 CSV 选中后压缩为普通 ZIP，即为导入包。
5. 进入追因顶部“批量测试”，选 CSV 或 ZIP，先核对预览，再点击“开始全部测试”。模型连接与指令在工作台配置。

## 字段怎么填

设备名称：例如会议投影仪。库存：非负整数。借用数量：正整数。
业务场景：工单模板固定填 ticket。工单标题：需要创建的内容。优先级：low、normal、high、urgent。
请求编号：每个独立任务使用不同编号，例如 my-test-001、my-test-002；整包都要避免重复。重复编号可能被标为重复或冲突。
种子：可固定42，不需要每次修改。故障：留空表示无故障；多种故障用中文分号“；”或英文分号“;”隔开，不使用顿号或逗号分隔。

支持的故障名称：提交后响应丢失、读取过期状态、响应内容缺失、工具调用延迟、写入前超时、未提交却返回成功、业务内容损坏。

不要改表头，不要新增“预期答案”“API密钥”等列。模型地址、密钥和指令不要放进包里。
现有批量入口最多200个上传文件、展开后500条逻辑用例（含重复和错误）；单个数据文件最多2MiB，ZIP最多20MiB、展开总计32MiB。以页面预览的限制为准。大量数据可分成多个CSV再打包。
“导入”单文件入口每文件最多20个场景；大批量应使用顶部“批量测试”。

## 次数与结果

每个场景会分别运行4种防护方式。这份5场景模板对应20次任务执行，不是20次模型请求；一次任务可能多次请求模型。请求额度不足会保留未运行或未完成记录。
ZIP里只放要测的CSV、JSON或JSONL数据即可。说明文件不是用例；日志只作静态检查，不执行Agent。参考答案另存，避免与输入混装。
先保留首轮结果，再修改模板复测；改过数据后不要当成同一份未修改的首轮数据。
"""
(DEST / "制作说明.md").write_text(instructions, encoding="utf-8")

with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for name in ("设备场景.csv", "工单场景.csv", "制作说明.md"):
        archive.write(DEST / name, name)

plan = prepare_uploads([{"filename": archive_path.name, "data": archive_path.read_bytes()}], profile="agent")
assert len(plan["cases"]) == 5
assert all(case["status"] == "ready" and case["kind"] == "scenario" for case in plan["cases"])
assert plan["counts"]["import_errors"] == 0
assert len({case["request_id"] for case in plan["cases"]}) == 5
summary = {"archive": str(archive_path), "counts": plan["counts"],
           "scope": "公开练习模板，仅使用当前批量解析器进行预览；未执行任务，未调用模型。"}
(DEST / "导入预览检查.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
