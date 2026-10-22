"""Convert the approved logo to application assets; do not redraw the artwork."""
from pathlib import Path
import argparse
import hashlib
import json
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent/'outputs/品牌设计/追因-logo-方案03-ZY连接圆环-20261020.png'
BUNDLED_SOURCE = ROOT/'packaging/assets'/SOURCE.name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, help='Approved source artwork; defaults to the workspace original or bundled copy')
    parser.add_argument('--output-root', type=Path, default=ROOT, help='Project root receiving static/assets and packaging/assets')
    args = parser.parse_args()
    source = args.source if args.source is not None else SOURCE if SOURCE.is_file() else BUNDLED_SOURCE
    source = source.resolve()
    if not source.is_file():
        parser.error('Approved source artwork was not found; pass --source or restore packaging/assets/'+SOURCE.name)
    output_root = args.output_root.resolve()
    image = Image.open(source).convert('RGBA')
    # Trim only transparent exterior, retaining the approved mark and its alpha.
    box = image.getchannel('A').point(lambda value: 255 if value >= 160 else 0).getbbox()
    if box is None:
        raise ValueError('Approved logo has no visible artwork')
    image = image.crop(box)
    edge = max(image.size)
    padding = round(edge * .025)
    square = Image.new('RGBA', (edge+padding*2, edge+padding*2), (0,0,0,0))
    square.alpha_composite(image, ((square.width-image.width)//2, (square.height-image.height)//2))
    square = square.resize((512,512), Image.Resampling.LANCZOS)
    web = output_root/'static/assets'
    native = output_root/'packaging/assets'
    web.mkdir(exist_ok=True, parents=True)
    native.mkdir(exist_ok=True, parents=True)
    square.save(web/'zhuiyin-logo.png', optimize=True)
    square.resize((48,48),Image.Resampling.LANCZOS).save(web/'favicon.png', optimize=True)
    square.save(native/'zhuiyin.ico', sizes=[(x,x) for x in (16,20,24,32,40,48,64,128,256)])
    (web/'favicon.ico').write_bytes((native/'zhuiyin.ico').read_bytes())
    report = {'approved_source':source.name, 'approved_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
              'source_size':Image.open(source).size, 'transparent_trim_box':box,
              'conversion':'transparent exterior trim, symmetric padding, resampling and ICO encoding only',
              'ico_sizes':sorted(Image.open(native/'zhuiyin.ico').ico.sizes())}
    (native/'icon-provenance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    main()
