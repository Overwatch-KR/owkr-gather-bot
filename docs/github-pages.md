# 공개 정책 사이트 이전

정책 사이트와 원본은 [OWKR Docs](https://overwatch-kr.github.io/owkr-docs/gather-bot)에서 함께 관리합니다.
기존 `https://overwatch-kr.github.io/owkr-gather-bot/` 주소를 사용하는 링크를 위해
이 저장소의 Pages는 이동 안내만 유지합니다. 정책 본문·스타일을 별도로 관리하거나 배포하지 않습니다.

## 기존 주소 호환

`scripts/build_pages.py`는 `_site/`에 다음 이동 페이지만 생성합니다.

| 기존 파일 | OWKR Docs 대상 |
| --- | --- |
| index.html | /owkr-docs/gather-bot |
| privacy.html | /owkr-docs/gather-bot/privacy |
| terms.html | /owkr-docs/gather-bot/terms |
| contact.html, deletion.html | /owkr-docs/gather-bot/support |

자동 이동이 차단되어도 본문의 링크로 이동할 수 있습니다. `.github/workflows/pages.yml`은
main push와 수동 실행에서 이 이동 안내만 배포합니다. 실패 시 이전 배포가 유지되며
Actions 로그를 확인하고 다시 실행합니다. 잘못된 대상은 수정 PR로 복구합니다.

공개 준비·시행일·초안 해제는 [OWKR Docs 관리 안내](https://github.com/Overwatch-KR/owkr-docs/blob/main/docs/gather-bot/README.md)를 따릅니다.
최종 정책 확정 전까지 운영 설정의 정책 URL을 최종 정책으로 등록하지 않습니다.
봇 서버·DB·백업은 이 이동 안내 배포와 무관하며 원본 이력은 Git에 보존됩니다.
