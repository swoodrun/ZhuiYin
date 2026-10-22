"""Add current-delivery notices to working historical HTML copies only."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
STAGES = {"配对证据演示.html": "原30条冻结判断与受控配对机制",
          "答辩演示.html": "早期同错案例及八组自由执行"}


def align(root=ROOT):
    changed = []
    for name, stage in STAGES.items():
        path = Path(root) / name
        text = path.read_text(encoding="utf-8")
        notice = ('<aside id="delivery-alignment-banner" style="padding:14px 22px;background:#e9f0eb;'
                  'color:#284956;border-bottom:1px solid #cbd9d0;font:14px/1.7 Microsoft YaHei,Segoe UI,sans-serif">'
                  f'<strong>历史阶段回放：{stage}。</strong>本页数据按原阶段保留，不是新的模型运行。'
                  '当前最新研究是24次证据资格判断，引用定位0/12；专项累计355次、1.050345元。'
                  '<a href="证据资格演示.html" style="color:#245f78">打开当前研究</a> · '
                  '<a href="当前交付口径.md" style="color:#245f78">统一口径</a></aside>')
        text = re.sub(r'<aside id="delivery-alignment-banner"[\s\S]*?</aside>', '', text)
        text, count = re.subn(r'(<body\b[^>]*>)', lambda m: m.group(1) + notice, text, count=1)
        if count != 1:
            raise ValueError("working HTML lacks a body: " + name)
        path.write_text(text, encoding="utf-8")
        changed.append(name)
    return changed


if __name__ == "__main__":
    print(align())
