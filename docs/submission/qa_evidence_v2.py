"""Check material integrity and render review sheets without editing PDFs."""
import hashlib
import json
from pathlib import Path
import fitz
from PIL import Image, ImageChops, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'dist/final/20261111_v2'
WORK = ROOT / '_work/submission_20261111_v2'


def sheets(label, paths, cols=2, width=650):
    result = []
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 18)
    for start in range(0, len(paths), 6):
        batch = paths[start:start + 6]
        height = max(round(Image.open(p).height * width / Image.open(p).width) for p in batch)
        canvas = Image.new('RGB', (cols * width, ((len(batch) + cols - 1) // cols) * (height + 26)), '#d9ddda')
        draw = ImageDraw.Draw(canvas)
        for i, path in enumerate(batch):
            image = Image.open(path).convert('RGB')
            image.thumbnail((width - 8, height))
            x, y = (i % cols) * width, (i // cols) * (height + 26)
            canvas.paste(image, (x + 4, y + 26))
            draw.text((x + 10, y + 3), label + ' ' + path.stem, font=font, fill='black')
        dest = WORK / f'{label}-contact-{start // 6 + 1}.jpg'
        canvas.save(dest, quality=92)
        result.append(str(dest.relative_to(ROOT)))
    return result


def main():
    manifest = json.loads((OUT / 'source_manifest.json').read_text(encoding='utf-8'))
    report = {'pdfs': {}, 'unchanged_visual_pages': {}, 'contact_sheets': [], 'visual_review': 'pending_primary_agent'}
    for file, expected in manifest['qa'].items():
        path = OUT / file
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected['sha256']
        document = fitz.open(path)
        assert len(document) == expected['pages']
        bounds = []
        for i, page in enumerate(document):
            text = page.get_text()
            assert len(text.strip()) > 80 and '\ufffd' not in text
            for block in page.get_text('dict')['blocks']:
                for line in block.get('lines', []):
                    for span in line['spans']:
                        rect = fitz.Rect(span['bbox'])
                        if not page.rect.contains(rect):
                            bounds.append({'page': i + 1, 'text': span['text'], 'rect': list(rect)})
        assert not bounds, bounds
        report['pdfs'][file] = {'pages': len(document), 'sha256_passed': True, 'text_bounds_passed': True}
    for label, count, first in [('plan', 16, 2), ('slides', 13, 1), ('evidence', 13, 1)]:
        same = []
        for i in range(first, count + 1):
            old = Image.open(ROOT / f'_work/submission_20261111/{label}/page-{i:02d}.png').convert('RGB')
            new = Image.open(WORK / f'{label}/page-{i:02d}.png').convert('RGB')
            if old.size == new.size and ImageChops.difference(old, new).getbbox() is None:
                same.append(i)
        report['unchanged_visual_pages'][label] = same
    report['contact_sheets'] += sheets('supplement', sorted((WORK / 'supplement').glob('page-*.png')))
    report['contact_sheets'] += sheets('plan-new', [WORK / f'plan/page-{i:02d}.png' for i in [1, *range(17,24)]])
    report['contact_sheets'] += sheets('slides-new', [WORK / f'slides/page-{i:02d}.png' for i in range(14,18)])
    # Also inspect any unexpectedly changed inherited page.
    for label, count, first in [('plan', 16, 2), ('slides', 13, 1), ('evidence', 13, 1)]:
        different = [WORK / f'{label}/page-{i:02d}.png' for i in range(first,count + 1)
                     if i not in report['unchanged_visual_pages'][label]]
        if different:
            report['contact_sheets'] += sheets(label + '-inherited-changed', different)
    (OUT / 'qa_v2.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
