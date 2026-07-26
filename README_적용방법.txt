리셀 PICK v6.11.5 적용 방법

1. ZIP 안의 파일을 GitHub 저장소 최상단(app.py가 있는 위치)에 모두 덮어씁니다.
2. GitHub Desktop에서 Commit to main → Push origin을 누릅니다.
3. Render 배포가 Live가 된 뒤 /version.json에서 6.11.5를 확인합니다.
4. 최초 한 번 로그인합니다.
5. 새로고침 후 로그인 유지, 뒤로가기로 종료 후 1~5분 내 재접속 시 로그인 유지를 확인합니다.

중요: app.py와 index.html만 따로 교체하지 말고 ZIP 전체를 덮어써야 전용 복원 쿠키와 세션 마이그레이션이 함께 적용됩니다.
