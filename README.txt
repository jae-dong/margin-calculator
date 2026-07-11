추가 기능:
- 사진 한 번 촬영 후 상품명 인식 및 쿠팡 검색 자동 실행
- 상품명 직접 입력 후 쿠팡 검색
- 음성 상품명 인식 후 쿠팡 검색 자동 실행
- 검색 결과 기반 가격 후보 확인 및 판매가 입력

가격 후보는 검색 결과 기반 보조 정보라 실제 쿠팡 상품 구성과 가격 확인이 필요합니다.

GitHub에서 기존 파일을 새 파일로 덮어쓰고 Commit changes 후
Render에서 Manual Deploy > Deploy latest commit을 누르세요.

Render 환경변수에 추가:
OPENAI_SEARCH_MODEL = gpt-4.1
