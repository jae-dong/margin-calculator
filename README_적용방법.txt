리셀 PICK v6.2.0 적용 안내

이번 버전 핵심
- PostgreSQL 구조 유지
- Render psycopg 설치 오류 수정
- Python 3.11.9 고정(.python-version 포함)
- Render의 postgresql:// 연결문자열을 Psycopg 3용으로 자동 변환
- 데이터베이스 연결 재시도와 /health 실제 DB 점검
- 회원별 월 분석 제공량을 서버에서 원자적으로 차감
- 저장된 분석 결과 재사용 시 횟수 미차감
- AI 오류 발생 시 차감 횟수 자동 복구
- 판매가·소싱가 자동입력 금지 유지
- 보안 응답 헤더와 외부 출처 쓰기요청 차단

GitHub Desktop 적용
1. 이 폴더 안의 파일을 리셀 PICK 프로젝트 폴더에 전부 덮어씁니다.
2. GitHub Desktop에서 리셀 PICK 저장소가 선택됐는지 확인합니다.
3. Summary에 '리셀 PICK v6.2.0 배포 안정화'를 입력합니다.
4. Commit to main을 누른 뒤 Push origin을 누릅니다.

Render 확인
1. Render의 리셀 PICK Web Service를 엽니다.
2. Environment에서 PYTHON_VERSION이 3.11.9인지 확인합니다. 없으면 추가합니다.
3. OPENAI_API_KEY가 입력되어 있는지 확인합니다.
4. PostgreSQL을 아직 만들지 않았다면 Render에서 생성하고 DATABASE_URL을 Web Service에 연결합니다.
5. Manual Deploy > Clear build cache & deploy를 한 번 실행합니다.
6. 배포 성공 후 웹주소 뒤에 /health를 붙여 ok:true, version:6.2.0, database:postgresql이 표시되는지 확인합니다.

중요
- REQUIRE_LOGIN_FOR_AI는 테스트 중에는 0으로 두었습니다. 베타 회원만 분석하도록 제한할 때 1로 바꿉니다.
- 이메일 인증·비밀번호 재설정 메일·실제 결제 연동은 아직 구현 전입니다.
- 이용약관과 개인정보처리방침 초안은 정식 판매 전에 최신 법령·Google Play 정책에 맞춰 별도 최종 검토해야 합니다.
- 실제 고객 데이터를 받기 전 회원가입, 로그인, 저장, 다른 기기 복원, 탈퇴, 분석 한도, DB 백업을 순서대로 검증해야 합니다.


[v6.2.0 추가 환경변수 - 공개 베타 전 설정]
REQUIRE_EMAIL_VERIFICATION=1
MAX_REGISTRATIONS_PER_IP_DAY=3
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=인증메일 발송용 Gmail 주소
SMTP_PASSWORD=Gmail 앱 비밀번호(일반 로그인 비밀번호 금지)
SMTP_FROM=인증메일 발송용 Gmail 주소

주의: 이메일만으로 서로 다른 Google 계정 생성을 100% 막을 수는 없습니다. 이 버전은 Gmail 점(.)·+별칭 통합, 이메일 인증, IP별 가입 제한, 미인증 계정의 AI 분석 차단을 함께 적용합니다. 정식 앱 단계에서는 Google Play 결제계정, Play Integrity, 휴대전화 인증 또는 결제수단 기준의 무료체험 1회 정책을 추가해야 합니다.
