리셀 PICK WEB v6.4.0 적용 방법

[핵심 업데이트]
- v6.3.2의 6자리 숫자 회원번호 기능 전체 포함
- 회원번호 포함 전체 회원목록 CSV 저장
- 로그인 5회 연속 실패 시 15분 계정 보호 잠금
- 정상 로그인 시 실패 횟수와 잠금 자동 해제
- 기존 PostgreSQL 회원에게도 새 보안 컬럼 자동 추가
- 관리자만 회원 CSV 다운로드 가능
- 회원목록 저장 작업을 관리자 작업 기록에 남김

[적용 순서]
1. 이 압축파일을 별도 폴더에 압축 해제
2. RESALE_PICK_v6.4.0 폴더 안의 파일 전체를 기존 margin-calculator 프로젝트 폴더에 덮어쓰기
3. GitHub Desktop에서 margin-calculator 저장소 선택
4. Summary: 리셀 PICK v6.4.0 회원번호·회원목록·로그인보안
5. Commit to main
6. Push origin
7. Render 배포가 Live가 될 때까지 기다리기
8. 웹앱 Ctrl+F5 강력 새로고침
9. 관리자 로그인 → 설정 → 운영자 관리 → 회원목록 저장(CSV) 테스트

[주의]
- DATABASE_URL, SECRET_KEY, ADMIN_EMAILS 등 기존 Render 환경변수는 그대로 유지하세요.
- 회원번호는 탈퇴 후에도 재사용하지 않는 고유번호입니다.
- CSV 파일에는 회원 이메일이 포함되므로 외부 공유 금지, 접근 권한이 있는 PC에만 보관하세요.
