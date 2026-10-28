"""Legacy URLs redirect without publishing stale policies or private files."""
from html.parser import HTMLParser

from scripts import build_pages


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in {'href', 'src'}:
                self.links.append(value)


def test_legacy_policy_urls_redirect_only_to_canonical_docs(tmp_path, monkeypatch):
    monkeypatch.setattr(build_pages, 'OUT', tmp_path / 'public')
    build_pages.OUT.mkdir()
    (build_pages.OUT / 'stale-privacy.md').write_text('stale policy')
    build_pages.build()
    assert {p.name for p in build_pages.OUT.iterdir()} == {
        'index.html', 'privacy.html', 'terms.html', 'contact.html',
        'deletion.html', '.nojekyll',
    }
    expected = {
        'index': '', 'privacy': '/privacy', 'terms': '/terms',
        'contact': '/support', 'deletion': '/support',
    }
    for slug, suffix in expected.items():
        html = (build_pages.OUT / f'{slug}.html').read_text()
        target = 'https://overwatch-kr.github.io/owkr-docs/gather-bot' + suffix
        parser = Links()
        parser.feed(html)
        assert parser.links == [target, target]
        assert f'content="0; url={target}"' in html
        assert 'noindex' in html
        assert '<script' not in html
