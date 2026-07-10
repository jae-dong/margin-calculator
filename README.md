# 마진계산기 AI 상품인식 버전

제품 앞면 사진을 촬영하면 AI가 브랜드명, 제품명, 용량을 인식하고 쿠팡 검색어를 만듭니다.
인식 결과는 사용자가 수정할 수 있으며, 쿠팡 검색 버튼을 누르면 검색 결과가 열립니다.

## 실행 방법

1. Python 3.10 이상을 설치합니다.
2. 이 폴더에서 다음 명령을 실행합니다.

```bash
pip install -r requirements.txt
```

3. OpenAI API 키를 환경변수로 설정합니다.

Windows PowerShell:
```powershell
$env:OPENAI_API_KEY="본인의_API키"
python app.py
```

macOS / Linux:
```bash
export OPENAI_API_KEY="본인의_API키"
python app.py
```

4. 브라우저에서 `http://localhost:5000`을 엽니다.

## 휴대폰에서 사용

카메라 촬영은 HTTPS 웹주소에서 가장 안정적으로 작동합니다.
Render, Railway, Fly.io 같은 웹 호스팅에 이 폴더를 배포하고
환경변수 `OPENAI_API_KEY`를 서버 설정에 등록하세요.

API 키는 HTML 파일에 직접 넣으면 안 됩니다.
AI가 인식한 상품명은 실제 제품의 용량·맛·구성과 다를 수 있으므로 쿠팡 검색 전에 확인하세요.
