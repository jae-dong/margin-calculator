리셀 PICK v6.18.0 적용 방법

1. ZIP 압축을 풉니다.
2. ZIP 안의 파일을 app.py가 있는 GitHub 저장소 최상단에 전부 덮어씁니다.
3. GitHub Desktop Summary에 아래 제목을 입력합니다.
   리셀 PICK v6.18.0 스토어 심사·계정삭제 공개페이지 업데이트
4. Commit to main → Push origin을 누릅니다.
5. Render 배포가 Live가 된 뒤 /version.json에서 6.18.0을 확인합니다.
6. 아래 공개 주소가 모두 열리는지 확인합니다.
   /privacy
   /terms
   /account-delete
7. 관리자 설정의 공개 정책·계정 삭제 주소에서 주소 점검을 누릅니다.

주의
- index.html만 교체하지 말고 ZIP 안의 파일을 전부 덮어쓰세요.
- v6.12.2에서 정상화된 로그인 유지 핵심 코드는 변경하지 않았습니다.
- 공개 계정 삭제 페이지는 실제 회원정보를 영구 삭제하므로 테스트 계정으로 먼저 확인하세요.
