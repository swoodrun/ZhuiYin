# 追因产品源码与复现入口

这是 2026-10-22 整理的 v0.7.0 源码快照，对应 Windows 程序构建 45545df053be38b5fcfb。构建清单中的 57 份输入文件与源码逐字节匹配。源码为本次提交快照，不含 Git 历史。

## 运行

后端只需 Python 标准库，已验证 Python 3.14.5，路径 `C:\Python314\python.exe`。进入本目录执行：

    C:\Python314\python.exe -B server.py --port 8766

然后打开 http://127.0.0.1:8766。启动、浏览、导入预览、固定规则检查和历史读取不需要模型密钥。真实 Agent 实验、模型批量测试、重复对比和连接测试需填写兼容 API 地址、模型名称和有效密钥，运行会使用服务额度。评审材料不附带 API 密钥或用户历史。

完整内嵌窗口请运行仓库根目录的“program/追因.exe”。程序无需另装 Python，需 Windows x64 和 WebView2 Runtime。

## 产品范围

工作台可设置模型、任务、故障、指令和预算；批量测试按任务比较四种防护方式；导入读取场景与日志，并提供只读详情；对比测试进行多轮实验；历史支持只读回看与比较；使用方法中保留免费规则自检。诊断、任务模板和选中任务重测已包含；长摘要换行和导入详情留在导入页的修复也已包含。

## 源码导航

- `engine.py`：本机设备借用、工单业务、工具执行、故障注入与独立业务判定。
- `agent_suite.py`、`test_suite.py`、`ingest.py`：批量 Agent 测试、规则批次与数据解析。
- `study.py`：多轮重复对比；`model_budget.py`、`run_billing.py`：调用额度与费用估算。
- `diagnostics.py`、`experiment_compare.py`、`task_templates.py`、`product_workflows.py`、`retest_templates.py`：诊断、历史比较、模板和重测。
- `server.py`、`job_store.py`：本机接口与记录保存。
- `static/`：页面、交互、样式与图标；`templates/`：公开练习模板。
- `desktop_entry.py`、`desktop_shell.py`、`runtime_paths.py`、`packaging/`：桌面宿主与数据目录。
- `tools/`：构建、前端合成 DOM 检查及研究辅助脚本；根目录 `test_*.py` 保留当前测试源码。

## 可移植离线测试

在本目录执行：

    C:\Python314\python.exe -B run_portable_tests.py

或双击“运行离线测试.cmd”。此入口选择 51 个不依赖完整历史研究目录的模块，测试使用新建合成数据和本机回环响应。前端测试另需 Node.js；Windows 桌面测试在 Windows 上运行。运行后上级目录生成“离线测试结果.json”，实际项数、失败、错误和跳过数以该文件为准。

834 项全量测试属于原工作区的历史验证；本次提交副本实际重跑 51 个模块、573 项。当前副本未复制整棵研究 data；部分历史研究测试依赖原始材料或旧路径，需在对应环境运行。

研究结果在项目说明中汇总，本包不附原始研究材料。产品回归和视频中的真实模型运行分别记录。

## 桌面重建

运行后端不需要构建工具。重建 Windows 便携 EXE 时，使用 Python 3.14.5 和 `requirements-build.txt` 中的 PyInstaller 6.22.3，安装 Windows .NET Framework 编译环境，再执行：

    C:\Python314\python.exe -B tools/build_desktop.py --format onedir

上级 `.pkg/webview2-sdk-1.0.4022.49/sdk/` 已保留构建需要的官方 SDK 程序集、x64 加载器和许可原文；系统仍需 WebView2 Runtime。本次交付使用 onedir 内嵌窗口；当前运行入口以此为准，历史单文件引导和 Qt 诊断源码仅作留存。


