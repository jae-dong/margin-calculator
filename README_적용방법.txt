리셀 PICK WEB v6.3.0 적용 방법

[핵심 업데이트]
- 관리자 권한을 DB role로 분리하고 ADMIN_EMAILS 계정만 서버 시작 시 관리자 승격
- 일반 회원에게 운영자 화면 완전 숨김, 관리자 API 403 차단
- 관리자 대시보드: 전체/유료/오늘가입/인증/중지/월분석 현황
- 회원 검색 및 무료·PRO·PRO+ 이용권/사용기간 적용
- 회원 이용 중지·재개, 즉시 기존 로그인 세션 해제
- 관리자 작업 이력 50건 저장
- 기존 비밀번호 찾기·회원탈퇴·PostgreSQL 기능 유지

[업데이트]
1. ZIP 압축 해제
2. RESALE_PICK_v6.3.0 폴더 안의 파일 전체를 기존 margin-calculator 프로젝트 폴더에 덮어쓰기
3. GitHub Desktop에서 margin-calculator 저장소 선택
4. Summary: 리셀 PICK v6.3.0 관리자 대시보드 및 회원관리
5. Commit to main → Push origin
6. Render 자동 배포 완료 확인
7. 관리자 계정으로 로그인 후 설정 → 운영자 관리에서 회원 조회 테스트

[환경변수 유지]
DATABASE_URL, SECRET_KEY, ADMIN_EMAILS, OPENAI_API_KEY 등 기존 값은 그대로 유지합니다.

[주의]
- 관리자 계정은 회원가입 화면에서 지정할 수 없습니다. Render의 ADMIN_EMAILS에 등록된 이메일만 DB role=admin으로 동기화됩니다.
- 실제 결제 자동 연동 전에는 관리자가 이용권을 수동으로 부여하는 베타 운영 방식입니다.
