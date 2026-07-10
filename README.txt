마진계산기 AI 상품인식 수정본

이번 수정본은 templates 폴더를 사용하지 않고 index.html을 루트에서 직접 열도록 만들었습니다.
따라서 Render에서 Not Found가 뜨던 문제를 피할 수 있습니다.

GitHub에서 기존 파일을 모두 지우고 아래 파일 5개를 저장소 맨 위에 올리세요.

app.py
index.html
requirements.txt
Procfile
render.yaml

API 키는 GitHub에 넣지 말고 Render의 Environment에서만 유지하세요.

업로드 후 Render에서:
Manual Deploy → Deploy latest commit

정상 확인 주소:
메인 화면: /
서버 상태: /health
