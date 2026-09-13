"""Render the generated n8n label parameters and check their visible centring.

Requires ImageMagick, Node.js and an installed Arial Regular font. Uses the
same draw/text primitives and baseline origin as Edit Image v1 in n8n 2.35.4.
No n8n execution, model generation, upload or publication is started.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess

from PIL import Image


ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font', type=Path, required=True)
    parser.add_argument('--magick', default=shutil.which('magick'))
    args = parser.parse_args()
    if not args.magick or not args.font.is_file():
        parser.error('ImageMagick and a readable Arial Regular font are required')
    workflow = json.loads((ROOT / 'n8n/book-promotion-review.json').read_text(encoding='utf-8'))
    nodes = {n['name']: n for n in workflow['nodes']}
    node = nodes['Add hero chapter label']
    assert node['typeVersion'] == 1, 'n8n 2.35.4 only provides Edit Image v1'
    assert node['parameters']['operation'] == 'text', 'A default border operation cannot draw a label'
    assert not {'horizontalAlignment', 'verticalAlignment'} & node['parameters'].keys()
    cases = [4, 18, 250, None]
    # Evaluate the real JSON expressions with n8n's item shape, rather than
    # duplicating the production layout calculation in the test.
    js = """
    const fs = require('node:fs');
    const {parameters, cases} = JSON.parse(fs.readFileSync(0, 'utf8'));
    console.log(JSON.stringify(cases.map(chapter => {
      const $json = {book_profile: {chapter_position: chapter}};
      return Object.fromEntries(Object.entries(parameters).map(([key, value]) =>
        [key, typeof value === 'string' && value.startsWith('={{')
          ? new Function('$json', 'return (' + value.slice(3, -2) + ');')($json) : value]));
    })));
    """
    result = subprocess.run([shutil.which('node'), '-e', js],
        input=json.dumps({'parameters': node['parameters'], 'cases': cases}),
        text=True, capture_output=True, check=True)
    params = json.loads(result.stdout)
    panel = nodes['Hero chapter panel']['parameters']
    left, top = panel['startPositionX'], panel['startPositionY']
    right, bottom = panel['endPositionX'], panel['endPositionY']
    centre = ((left + right) / 2, (top + bottom) / 2)
    output = ROOT / 'outputs/n8n/label-verification'
    output.mkdir(parents=True, exist_ok=True)
    tiles = []
    for chapter, p in zip(cases, params):
        path = output / f'label-{chapter or "fallback"}.png'
        # Text is a controlled literal (Kapitel + integer, or Buchauszug).
        drawing = f"text {p['positionX']},{p['positionY']} '{p['text']}'"
        subprocess.run([args.magick, '-size', '1080x1350', 'xc:#304050',
            '-fill', panel['color'], '-draw',
            f"roundrectangle {left},{top} {right},{bottom} {panel['cornerRadius']},{panel['cornerRadius']}",
            '-font', str(args.font), '-pointsize', str(p['fontSize']),
            '-fill', p['fontColor'], '-draw', drawing, str(path)], check=True, capture_output=True)
        with Image.open(path) as image:
            white = image.convert('RGB').split()[0].point(lambda x: 255 if x > 180 else 0)
            bounds = white.getbbox()
            assert bounds is not None, f'{p["text"]}: text missing'
            x1, y1, x2, y2 = bounds
            actual = ((x1 + x2 - 1) / 2, (y1 + y2 - 1) / 2)
            assert left < x1 < x2 < right and top < y1 < y2 < bottom, bounds
            assert abs(actual[0] - centre[0]) <= 2 and abs(actual[1] - centre[1]) <= 2, (p['text'], actual, centre)
            print(f'{p["text"]}: visible centre {actual}; target {centre}')
            tiles.append(image.convert('RGB').crop((left - 16, top - 16, right + 16, bottom + 16)))
    preview = Image.new('RGB', (tiles[0].width * 2, tiles[0].height * 2))
    for i, tile in enumerate(tiles):
        preview.paste(tile, ((i % 2) * tile.width, (i // 2) * tile.height))
    preview.save(output / 'preview.png')
    print('Four rendered chapter labels are visible and centred within 2 px.')


if __name__ == '__main__':
    main()
