리셀 PICK v6.0.0 적용 안내

핵심 변경
- PostgreSQL 상용 데이터베이스 연결
- 여러 사용자·여러 기기 동시 사용 충돌 방지
- 회원별 데이터 완전 분리
- 비밀번호 변경·회원 탈퇴 API
- 운영자 요약 API와 보안 쿠키 설정

Render 배포
1. 폴더 내용을 기존 GitHub 저장소에 덮어씁니다.
2. GitHub Desktop Summary: 리셀 PICK v6.0.0 PostgreSQL 전환
3. Commit to main 후 Push origin을 누릅니다.
4. Render Blueprint를 적용합니다.
5. OPENAI_API_KEY와 ADMIN_EMAILS 환경변수를 입력합니다.
6. 배포 후 /health를 확인합니다.

주의
- 기존 SQLite 베타 회원정보는 PostgreSQL로 자동 이전되지 않습니다.
- 이메일 인증과 비밀번호 찾기 메일 발송은 다음 단계입니다.
