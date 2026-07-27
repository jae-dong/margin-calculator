리셀 PICK v6.19.0 웹서버 적용 방법

1. ZIP 압축을 풉니다.
2. ZIP 안의 파일을 app.py가 있는 GitHub 저장소 최상단에 전부 덮어씁니다.
3. GitHub Desktop Summary:
   리셀 PICK v6.19.0 Android 베타 패키징 준비
4. Commit to main → Push origin을 누릅니다.
5. Render 배포가 Live가 된 뒤 /version.json에서 6.19.0을 확인합니다.
6. /api/mobile-shell-config 주소가 열리고 web_version이 6.19.0인지 확인합니다.

주의
- 정상 작동 중인 로그인 유지 핵심 로직은 수정하지 않았습니다.
- Android 앱 프로젝트의 app_url은 실제 Render 주소와 일치해야 합니다.
- index.html만 교체하지 말고 ZIP 전체를 덮어쓰세요.
