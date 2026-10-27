"""Build only public policy documents, without third-party dependencies."""
from html import escape
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '_site'
PAGES = [('privacy', '개인정보처리방침'), ('terms', '이용약관'), ('contact', '문의'), ('deletion', '삭제 요청')]


def inline(value):
    value = escape(value)
    value = re.sub(r'`([^`]+)`', r'<code>\1</code>', value)
    value = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', value)
    return value.replace('privacy.md', 'privacy.html').replace('terms.md', 'terms.html').replace('support.md', 'deletion.html')


def markdown(source):
    output, paragraph = [], []
    listing = None
    table = False
    def flush():
        if paragraph:
            output.append('<p>' + inline(' '.join(paragraph)) + '</p>')
            paragraph.clear()
    for line in source.splitlines() + ['']:
        if line.startswith('|'):
            flush()
            cells = [cell.strip() for cell in line.strip('|').split('|')]
            if all(re.fullmatch(r'[- :]+', cell) for cell in cells):
                continue
            tag = 'td' if table else 'th'
            if not table:
                output.append('<div class="table-scroll" role="region" aria-label="처리 정보 목록" tabindex="0"><table>')
                table = True
            output.append('<tr>' + ''.join(f'<{tag}>{inline(cell)}</{tag}>' for cell in cells) + '</tr>')
            continue
        if table:
            output.append('</table></div>')
            table = False
        item = re.match(r'(- |[0-9]+\. )(.*)', line)
        if item:
            flush()
            kind = 'ul' if item[1] == '- ' else 'ol'
            if listing != kind:
                if listing:
                    output.append(f'</{listing}>')
                output.append(f'<{kind}>')
                listing = kind
            output.append('<li>' + inline(item[2]) + '</li>')
            continue
        if listing:
            output.append(f'</{listing}>')
            listing = None
        if line.startswith('#'):
            flush()
            level = len(line) - len(line.lstrip('#'))
            output.append(f'<h{level}>{inline(line[level:].strip())}</h{level}>')
        elif line.startswith('>'):
            flush()
            output.append('<p class="notice">' + inline(line[1:].strip()) + '</p>')
        elif not line.strip():
            flush()
        else:
            paragraph.append(line)
    return '\n'.join(output)


def page(slug, title, body):
    nav = ''.join(f'<a href="{key}.html"' + (' aria-current="page"' if key == slug else '') + f'>{label}</a>' for key, label in PAGES)
    return f'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><meta name="description" content="OWKR Gather Bot의 개인정보, 이용약관, 문의 및 삭제 요청 안내">
<title>{escape(title)} · OWKR Gather Bot</title><link rel="stylesheet" href="style.css"></head>
<body><a class="skip" href="#content">본문으로 이동</a><header><a class="brand" href="index.html">OWKR Gather Bot</a><nav aria-label="안내 문서">{nav}</nav></header>
<main id="content">{body}</main><footer>운영 담당 qtaghdi · <a href="mailto:qtaghdi@gmail.com">qtaghdi@gmail.com</a><br>최종 검토일 2026-10-27 · 정책 시행일 미정</footer></body></html>'''


def build():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    shutil.copyfile(ROOT / 'site/style.css', OUT / 'style.css')
    documents = {name: (ROOT / f'docs/legal/{name}.md').read_text() for name in ('privacy', 'terms', 'support')}
    content = {'privacy': markdown(documents['privacy']), 'terms': markdown(documents['terms'])}
    content['contact'] = markdown('''# 문의 안내

오류·악용 신고와 개인정보 열람·수정·삭제·처리정지 요청을 받습니다.

## 연락처

- 운영 담당: qtaghdi (개발자 겸 내전 총관리자)
- 이메일: [qtaghdi@gmail.com](mailto:qtaghdi@gmail.com)
- 보조 연락 경로: Discord `qtaghdi`에게 비공개 메시지

Discord 접속이나 DM이 어려우면 이메일을 이용해 주세요. 공개 채널이나 GitHub 이슈에는 개인정보를 올리지 마세요.

## 문의에 포함할 내용

문제 상황과 발생 시각, 관련 메시지 링크를 필요한 범위에서 알려주세요. 스크린샷에는 다른 사람의 정보와 인증 정보를 가려 주세요.

비밀번호·봇 토큰·Discord 인증 토큰·신분증 사본은 보내지 마세요.

개인정보 삭제는 [삭제 요청 안내](deletion.html)를 확인해 주세요.
''')
    content['deletion'] = markdown(documents['support'].replace('# 문의·개인정보 수정·삭제 요청 안내', '# 개인정보 삭제 요청 안내'))
    for slug, title in PAGES:
        (OUT / f'{slug}.html').write_text(page(slug, title, content[slug]))
    links = '\n'.join(f'<li><a href="{slug}.html">{title}</a></li>' for slug, title in PAGES)
    home = f'''<h1>이용 안내</h1><p>OWKR Gather Bot의 개인정보 처리, 이용약관과 문의·삭제 요청 방법을 안내합니다.</p><p class="notice">개인정보처리방침과 이용약관은 현재 검토 중인 초안입니다. 운영 환경·로그 및 백업 보관 절차·시행일은 확정 후 갱신합니다.</p><ul class="documents">{links}</ul><p>온라인 접수 양식 없이 이메일 또는 Discord 비공개 메시지로 문의를 받습니다.</p>'''
    (OUT / 'index.html').write_text(page('index', '이용 안내', home))
    (OUT / '.nojekyll').touch()
    print('Built public documents in _site (5 pages and stylesheet).')


if __name__ == '__main__':
    build()
