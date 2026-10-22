"""Wrap fresh author-constructed inputs; never run application code or samples."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/field-tests/holdout-20261018-v1"
DESTINATION = ROOT / "data/deliveries"
FREEZE = ROOT / "data/delivery-alignment-20261018/code-freeze-before-new-inputs.json"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def dump(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def seal(path, members):
    members["校验清单.json"] = dump({name: {"sha256": sha(data), "bytes": len(data)} for name, data in sorted(members.items())})
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(members.items()):
            archive.writestr(name, data)
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == set(members)
        assert all(archive.read(name) == data for name, data in members.items())
    Path(str(path) + ".sha256").write_text(sha(path.read_bytes()) + "  " + path.name + "\n", encoding="utf-8")
    return {"name": path.name, "sha256": sha(path.read_bytes()), "members": len(members), "passed": True}


def build():
    DESTINATION.mkdir(parents=True, exist_ok=True)
    paths = [DESTINATION / "追因-新样本实测输入-20261018.zip",
             DESTINATION / "追因-新样本参考答案-测试后再开-20261018.zip"]
    acceptance = ROOT / "data/delivery-alignment-20261018/fresh-input-package-check.json"
    if acceptance.exists() or any(p.exists() or Path(str(p) + ".sha256").exists() for p in paths):
        raise FileExistsError("fresh packages must not overwrite a previous version")
    frozen = json.loads(FREEZE.read_text(encoding="utf-8"))
    changed = [name for name, expected in frozen["files"].items() if sha((ROOT / name).read_bytes()) != expected]
    if changed:
        raise ValueError("core code changed after freeze")
    manifest_path = SOURCE / "file-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))["files"]
    actual = {p.relative_to(SOURCE).as_posix() for p in SOURCE.rglob("*") if p.is_file() and p.name != "file-manifest.json"}
    if set(manifest) != actual:
        raise ValueError("fresh dataset file membership changed")
    for name, expected in manifest.items():
        path = (SOURCE / name).resolve()
        if not path.is_relative_to(SOURCE) or sha(path.read_bytes()) != expected:
            raise ValueError("fresh dataset hash changed")
    checks = json.loads((SOURCE / "format-checks.json").read_text(encoding="utf-8"))
    if (checks["application_import_status"] != "not_run" or checks["business_test_status"] != "not_run"
            or any(checks["project_runtime_calls"].values()) or not checks["record_actual_result_fields_all_blank"]):
        raise ValueError("fresh inputs cannot be presented as unexecuted")
    public = {name: (SOURCE / name).read_bytes() for name in actual
              if name.startswith(("inputs/", "records/")) or name in ("SOURCE-DECLARATION.json", "format-checks.json")}
    public["代码冻结.json"] = FREEZE.read_bytes()
    public["先读我.md"] = ("# 新样本实测输入\n\n"
        "这批数据在代码冻结后新生成，仅作标准库格式与哈希检查；未交给追因解析器、裁判、引擎或模型，实测结果全空。"
        "同一协作者构造、覆盖已有故障家族，不是外部独立验证或模型训练未见证明。\n\n"
        "1. 在原项目双击start.cmd，进入启动器打开的主工作台的‘导入’；不要使用8768一次性密钥页。无需密钥或连接测试。\n"
        "2. 上传inputs/scenarios.json，预览20场景后点‘批量验证’。rules模式不调用模型。设备和工单CSV各10行，与JSON是同20案例的备用格式，不额外计样本。\n"
        "3. 逐份上传inputs/traces/H01.json至H16.json，静态查看16份日志；日志工具不会执行。actual与最终快照是作者声明。\n"
        "4. 保存首次报告或截图，填写records中的两份空表。场景80行是20场景×4策略观察位置，不是80独立场景；页面未提供的指标留空。\n"
        "5. 完成首轮后再解压另一个‘追因-新样本参考答案-测试后再开-20261018.zip’。它给人工业务义务参考，不是软件已通过的结果。差异与异常也保留，后续修复另记版本。\n\n"
        "本包没有answers目录，也没有API配置、执行脚本或收费启动器。19份上传文件对应20场景+16日志，共36个实例。"
        "静态导入集不直接证明E0—E3资格研究或引用协议效果。SOURCE-DECLARATION.json与format-checks.json说明边界；"
        "校验清单.json核对包内字节，不代替应用验收。\n").encode("utf-8")
    if any(name.startswith("answers/") for name in public):
        raise ValueError("answer leakage into input package")
    private = {name: (SOURCE / name).read_bytes() for name in actual if name.startswith("answers/")}
    private["输入对应哈希.json"] = dump(checks["input_sha256"])
    private["代码冻结.json"] = FREEZE.read_bytes()
    private["SOURCE-DECLARATION.json"] = (SOURCE / "SOURCE-DECLARATION.json").read_bytes()
    private["先读我.md"] = ("# 完成首次实测后再看\n\n"
        "先填写输入包中的实测空表并保留导出或截图，再打开answers/README.md。"
        "参考按公开业务合同人工推导，未用追因实际输出反向生成，也不预填四策略表现。"
        "它不是第三方认证或真实系统结果。输入对应哈希.json将答案绑定到原36例；"
        "发现差异先保留，不修改输入或判据来抹掉首轮观察。\n").encode("utf-8")
    packages = [seal(paths[0], public), seal(paths[1], private)]
    # Recheck bytes only; never import or run any application evaluator.
    unchanged = all(sha((ROOT / name).read_bytes()) == expected for name, expected in frozen["files"].items())
    if not unchanged:
        raise ValueError("core changed while packaging")
    result = {"passed": True, "packages": packages, "source_manifest_sha256": sha(manifest_path.read_bytes()),
              "distinct_scenarios": 20, "distinct_traces": 16, "upload_files": 19,
              "input_package_contains_answers": False, "core_files_unchanged": len(frozen["files"]),
              "scope": "byte-level packaging and hashes only; no application import or test result",
              "application_import_runs": 0, "engine_runs": 0, "scorer_runs": 0, "model_requests": 0,
              "business_test_status": "not_run"}
    acceptance.write_bytes(dump(result))
    return result


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False))
