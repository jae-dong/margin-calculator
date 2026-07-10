import base64
import json
import os
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from openai import OpenAI

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}

@app.get("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")

@app.get("/health")
def health():
    return jsonify(status="ok")

@app.post("/api/recognize")
def recognize():
    image = request.files.get("image")
    if image is None:
        return jsonify(error="사진 파일이 없습니다."), 400

    mime_type = image.mimetype or "image/jpeg"
    if mime_type not in ALLOWED_TYPES:
        return jsonify(error="JPG, PNG, WEBP 사진만 지원합니다."), 400

    raw = image.read()
    if not raw:
        return jsonify(error="사진 파일이 비어 있습니다."), 400

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return jsonify(error="Render 환경변수 OPENAI_API_KEY가 등록되지 않았습니다."), 500

    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    encoded = base64.b64encode(raw).decode("ascii")
    data_url = f"data:{mime_type};base64,{encoded}"

    prompt = """
한국 마트에서 촬영한 판매 상품 사진을 분석하라.
사진에 실제로 보이는 글자와 상품 정보만 사용하고 추측하지 마라.
쿠팡에서 검색하기 좋은 검색어를 만들어라.

반드시 아래 JSON 형식 하나만 반환하라.
{
  "brand": "브랜드명 또는 빈 문자열",
  "product_name": "제품명 또는 빈 문자열",
  "variant": "맛, 향, 타입 또는 빈 문자열",
  "volume": "용량, 중량, 개수 또는 빈 문자열",
  "search_query": "브랜드 제품명 맛/향/타입 용량을 합친 한 줄 검색어",
  "confidence": "높음 또는 보통 또는 낮음"
}

사진만 보고 묶음 판매 수량을 알 수 없으면 임의로 넣지 마라.
"""

    try:
        client = OpenAI(api_key=api_key)
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
        return jsonify({
            "brand": result.get("brand", ""),
            "product_name": result.get("product_name", ""),
            "variant": result.get("variant", ""),
            "volume": result.get("volume", ""),
            "search_query": result.get("search_query", ""),
            "confidence": result.get("confidence", "보통")
        })
    except json.JSONDecodeError:
        return jsonify(error="AI 응답을 상품명 형식으로 읽지 못했습니다. 다시 촬영해 주세요."), 502
    except Exception as exc:
        return jsonify(error=f"AI 상품 인식 오류: {exc}"), 502

@app.errorhandler(404)
def not_found(_):
    return send_from_directory(BASE_DIR, "index.html")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
