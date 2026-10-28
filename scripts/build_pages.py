"""Keep legacy policy URLs working while OWKR Docs owns all policy content."""
from html import escape
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '_site'
POLICY_BASE = 'https://overwatch-kr.github.io/owkr-docs/gather-bot'
TARGETS = {
    'index': '',
    'privacy': '/privacy',
    'terms': '/terms',
    'contact': '/support',
    'deletion': '/support',
}


def build():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    for slug, suffix in TARGETS.items():
        target = escape(POLICY_BASE + suffix, quote=True)
        (OUT / f'{slug}.html').write_text(f'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<meta http-equiv="refresh" content="0; url={target}">
<link rel="canonical" href="{target}"><title>정책 안내 이전 · OWKR</title></head>
<body><main><h1>정책 안내를 OWKR Docs로 옮겼습니다</h1>
<p><a href="{target}">정책·지원 안내로 이동</a></p></main></body></html>''')
    (OUT / '.nojekyll').touch()
    print('Built legacy policy redirects to OWKR Docs.')


if __name__ == '__main__':
    build()
