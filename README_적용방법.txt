리셀 PICK v6.11.9 적용 방법

1. ZIP 안의 파일을 app.py가 있는 GitHub 저장소 최상단에 전부 덮어씁니다.
2. GitHub Desktop에서 Commit to main → Push origin 합니다.
3. Render 배포 상태가 Live가 된 뒤 앱 주소 뒤에 /version.json을 붙여 6.11.9인지 확인합니다.
4. 이번 버전은 로그인 저장 키가 새로 바뀌었으므로 최초 한 번 다시 로그인합니다.
5. 로그인 직후 새로고침 → 뒤로가기로 종료 → 다시 접속 순서로 확인합니다.

중요: index.html만 교체하면 서버 인증 구조가 함께 바뀌지 않으므로 ZIP 전체를 덮어써야 합니다.
