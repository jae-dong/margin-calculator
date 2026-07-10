import base64
import json
import os
from flask import Flask, jsonify, render_template, request
from openai import OpenAI

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}

@app.get("/")
def index():
    return render_template("index.html")

@app.post("/api/recognize")
def recognize():
    image = request.files.get("image")
    if not image:
        return jsonify(error="사진이 없습니다."), 400

    mime_type = image.mimetype or "image/jpeg"
    if mime_type not in ALLOWED_TYPES:
        return jsonify(error="JPG, PNG, WEBP 사진만 지원합니다."), 400

    raw = image.read()
    if not raw:
        return jsonify(error="빈 사진 파일입니다."), 400

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return jsonify(error="서버에 OPENAI_API_KEY가 설정되지 않았습니다."), 500

    encoded = base64.b64encode(raw).decode("ascii")
    data_url = f"data:{mime_type};base64,{encoded}"

    client = OpenAI(api_key=api_key)
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    prompt = """
한국 마트에서 촬영한 판매 상품 사진을 분석해 상품 검색어를 만들어라.
사진에 실제로 보이는 정보만 사용하고 추측하지 마라.

반드시 JSON 한 개만 반환:
{
  "brand": "브랜드명 또는 빈 문자열",
  "product_name": "제품명",
  "volume": "용량/중량/개수 표기 또는 빈 문자열",
  "variant": "맛/향/타입 또는 빈 문자열",
  "search_query": "쿠팡 검색에 적합한 한 줄 검색어",
  "confidence": "높음|보통|낮음"
}

search_query 순서:
브랜드 + 제품명 + 맛/향/타입 + 용량/중량.
사진 한 장만으로 묶음 판매 수량을 알 수 없으면 임의로 넣지 마라.
"""

    try:
        response = client.responses.create(
            model=model,
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": data_url}
                ]
            }],
            max_output_tokens=300
        )

        text = response.output_text.strip()
        if text.startswith("```"):
            text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)

        return jsonify(
            brand=result.get("brand", ""),
            product_name=result.get("product_name", ""),
            volume=result.get("volume", ""),
            variant=result.get("variant", ""),
            search_query=result.get("search_query", ""),
            confidence=result.get("confidence", "보통"),
        )
    except json.JSONDecodeError:
        return jsonify(error="AI 응답 형식을 읽지 못했습니다."), 502
    except Exception as exc:
        return jsonify(error=f"상품 인식 오류: {str(exc)}"), 502

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
