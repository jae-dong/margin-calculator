리셀 PICK v6.11.3 적용 방법

1. ZIP 안의 파일을 기존 GitHub 프로젝트에 전부 덮어씁니다.
2. GitHub Desktop에서 Commit 후 Push origin 합니다.
3. Render가 Live가 될 때까지 기다립니다.
4. 배포 주소의 /version.json에서 6.11.3을 확인합니다.
5. 휴대폰에서 기존 앱을 완전히 종료하고 다시 실행합니다.
6. 이번 버전 적용 후 최초 1회는 다시 로그인합니다. 그 뒤 새로고침 및 뒤로가기 종료/재실행을 시험합니다.

주의: index.html만 교체하면 안 됩니다. app.py와 sw.js가 함께 변경되었습니다.
