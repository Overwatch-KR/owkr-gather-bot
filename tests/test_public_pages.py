"""Verify the public artifact boundary and project-relative navigation."""
from html.parser import HTMLParser
from urllib.parse import urlsplit

from scripts import build_pages


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in {'href', 'src'}:
                self.links.append(value)


def test_public_artifact_contains_only_approved_documents(tmp_path, monkeypatch):
    monkeypatch.setattr(build_pages, 'OUT', tmp_path / 'public')
    build_pages.build()
    assert {p.name for p in build_pages.OUT.iterdir()} == {
        'index.html', 'privacy.html', 'terms.html', 'contact.html',
        'deletion.html', 'style.css', '.nojekyll',
    }
    for path in build_pages.OUT.glob('*.html'):
        parser = Links()
        parser.feed(path.read_text())
        for target in parser.links:
            url = urlsplit(target)
            if url.scheme:
                assert url.scheme == 'mailto'
            elif url.path:
                assert not url.path.startswith('/')
                assert (path.parent / url.path).is_file(), target
        assert '<script' not in path.read_text()
        assert '시행일 미정' in path.read_text()


def test_deletion_steps_remain_readable():
    html = build_pages.markdown('1. 본인 확인\n2. 처리 범위 확인\n\n다음 안내')
    assert '<ol>\n<li>본인 확인</li>\n<li>처리 범위 확인</li>\n</ol>' in html
    assert '<p>다음 안내</p>' in html
