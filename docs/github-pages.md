# 공개 안내 웹 (GitHub Pages)

봇 서버와 별도로 운영하는 정적 문서 사이트다. 로그인·접수 폼·DB·분석 스크립트·외부 폰트는 사용하지 않는다. 문의와 삭제 요청은 이메일 또는 Discord DM으로 받는다.

## 문서와 배포

- `docs/legal/privacy.md`, `terms.md`, `support.md`: 정책과 삭제 요청 안내 원본
- `scripts/build_pages.py`: 위 원본과 문의·목차를 HTML로 변환
- `site/style.css`: 모든 공개 페이지의 공통 스타일
- `_site/`: 생성된 배포물. Git에는 포함하지 않음
- `.github/workflows/pages.yml`: main 변경 또는 main에서 수동 실행 시 배포

공개 페이지는 `index.html`, `privacy.html`, `terms.html`, `contact.html`, `deletion.html`이다. `_site`만 업로드하므로 내부 검토 문서·설정·DB·백업을 배포하지 않는다.

```bash
python scripts/build_pages.py
python -m http.server 8768 --directory _site
```

예정 기본 주소는 `https://overwatch-kr.github.io/owkr-gather-bot/`이다. 저장소 Settings → Pages의 Source를 GitHub Actions로 설정한다. main에 반영된 뒤 Public policy pages 작업의 성공과 실제 HTTPS 접근을 확인한다. PR 브랜치는 배포하지 않는다.

## 초안과 최종 정책

현재 문서는 검토 중임을 페이지에 명시하고 검색 색인을 요청하지 않는다. 초안 URL을 최종 정책으로 Developer Portal이나 봇 설정에 등록하지 않는다. 호스팅·로그·백업·표시 요건·시행일 확인 후 원본의 초안 표시와 생성기의 noindex·목차 안내를 함께 갱신한다. 정적 사이트 호스팅은 GitHub Pages이며 봇 DB 호스팅과 구분한다. GitHub 측 접속 로그 등은 제공자의 정책을 확인하여 최종 문서에 반영한다.

최종 공개 확인 후 운영 설정의 `policy.privacy_url`과 `policy.terms_url`에 각 HTTPS 주소를 넣는다. 예제 설정의 null은 최종 정책 확정 전까지 유지한다.

## 장애·로그·복원

정적 문서는 상시 실행 프로세스가 없으므로 봇 자동 재시작 설정과 무관하다. 실패 원인은 Actions의 Public policy pages 로그에서 확인하며, 실패 시 main에서 작업을 재실행한다. 배포 로그에 비밀 값을 추가하지 않는다. 이미 게시된 사이트는 새 배포가 성공하기 전까지 이전 버전을 제공한다.

원본과 스타일은 Git 이력으로 보관한다. 잘못 게시한 내용은 해당 변경을 되돌리는 PR을 병합해 재배포한다. 서비스 중단 시 Settings → Pages에서 사이트 게시를 중지할 수 있다. 봇 SQLite 백업·복원은 기존 VM 운영 안내를 따른다.
