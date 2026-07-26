리셀 PICK v6.11.4 적용 방법

1. ZIP을 풉니다. 이 ZIP은 폴더 안이 아니라 파일들이 바로 들어 있습니다.
2. GitHub 저장소의 app.py, index.html, sw.js, version.json 등 기존 파일 위치에 모두 덮어씁니다.
3. GitHub Desktop에서 변경 파일이 여러 개 보이는지 확인합니다.
4. Commit to main → Push origin을 진행합니다.
5. Render가 Live가 된 뒤 배포 주소 뒤에 /version.json을 붙여 6.11.4인지 확인합니다.
6. 앱을 완전히 종료한 뒤 다시 실행합니다. 새 서비스워커 적용을 위해 첫 실행에서 한 번 자동 새로고침될 수 있습니다.

중요: GitHub 저장소 안에 RESALE_PICK_v6.11.4 폴더를 새로 넣지 말고, ZIP 내부 파일을 기존 app.py가 있는 저장소 최상단에 덮어써야 합니다.
