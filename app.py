import base64
import json
import os
import re
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


@app.get("/manifest.json")
def manifest():
    return send_from_directory(BASE_DIR, "manifest.json", mimetype="application/manifest+json")

@app.get("/sw.js")
def service_worker():
    return send_from_directory(BASE_DIR, "sw.js", mimetype="application/javascript")

@app.get("/icon-192.png")
def icon192():
    return send_from_directory(BASE_DIR, "icon-192.png")

@app.get("/icon-512.png")
def icon512():
    return send_from_directory(BASE_DIR, "icon-512.png")

@app.get("/health")
def health():
    return jsonify(status="ok")

def get_client():
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY가 등록되지 않았습니다.")
    return OpenAI(api_key=key)

def parse_json(text):
    text = text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(text)

@app.post("/api/recognize")
def recognize():
    image = request.files.get("image")
    if image is None:
        return jsonify(error="사진 파일이 없습니다."), 400

    mime = image.mimetype or "image/jpeg"
    if mime not in ALLOWED_TYPES:
        return jsonify(error="JPG, PNG, WEBP만 지원합니다."), 400

    raw = image.read()
    data_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    prompt = """
한국 마트 상품 사진을 분석해 쿠팡 검색어를 만들어라.
사진에 실제로 보이는 브랜드, 제품명, 맛/향/타입, 용량만 사용하고 추측하지 마라.
묶음 수량은 사진만으로 확실하지 않으면 넣지 마라.
반드시 JSON 하나만 반환:
{"brand":"","product_name":"","variant":"","volume":"","search_query":"","confidence":"높음|보통|낮음"}
"""

    try:
        response = get_client().responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            input=[{
                "role":"user",
                "content":[
                    {"type":"input_text","text":prompt},
                    {"type":"input_image","image_url":data_url}
                ]
            }],
            max_output_tokens=300
        )
        return jsonify(parse_json(response.output_text))
    except Exception as exc:
        return jsonify(error=f"상품 인식 오류: {exc}"), 502

@app.post("/api/find-price")
def find_price():
    body = request.get_json(silent=True) or {}
    query = str(body.get("query","")).strip()
    if not query:
        return jsonify(error="검색어가 없습니다."), 400

    prompt = f"""
현재 쿠팡 검색 결과에서 다음 상품과 가장 정확히 일치하는 상품 1개를 찾아라.
검색어: {query}
브랜드, 제품명, 용량, 맛/향, 구성 수량이 정확히 일치해야 한다.
와우전용가, 쿠폰가, 단위가격, 할인 전 가격은 제외한다.
현재 판매가격을 확신할 수 없으면 price를 0으로 반환한다.
반드시 JSON 하나만 반환:
{{"title":"","price":0,"confidence":"높음|보통|낮음","note":""}}
"""

    try:
        response = get_client().responses.create(
            model=os.getenv("OPENAI_SEARCH_MODEL", "gpt-4.1"),
            tools=[{"type":"web_search"}],
            input=prompt,
            max_output_tokens=250
        )
        result = parse_json(response.output_text)
        price = result.get("price", 0)
        if isinstance(price, str):
            price = int(re.sub(r"[^0-9]", "", price) or "0")
        result["price"] = int(price or 0)
        return jsonify(result)
    except Exception as exc:
        return jsonify(error=f"가격 자동 확인 실패: {exc}", price=0), 502


@app.post("/api/recognize-receipt")
def recognize_receipt():
    image = request.files.get("image")
    if image is None:
        return jsonify(error="사진 파일이 없습니다."), 400

    mime = image.mimetype or "image/jpeg"
    if mime not in ALLOWED_TYPES:
        return jsonify(error="JPG, PNG, WEBP만 지원합니다."), 400

    raw = image.read()
    data_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    prompt = """
한국 마트 영수증 사진을 분석하라.
사진에서 실제로 확인되는 내용만 사용하고 추측하지 마라.
반드시 JSON 하나만 반환:
{
  "store": "매장명 또는 빈 문자열",
  "date": "날짜 또는 빈 문자열",
  "total": 0,
  "items": [
    {"name":"상품명","qty":1,"amount":0}
  ],
  "confidence":"높음|보통|낮음"
}
"""

    try:
        response = get_client().responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            input=[{
                "role":"user",
                "content":[
                    {"type":"input_text","text":prompt},
                    {"type":"input_image","image_url":data_url}
                ]
            }],
            max_output_tokens=900
        )
        return jsonify(parse_json(response.output_text))
    except Exception as exc:
        return jsonify(error=f"영수증 인식 오류: {exc}"), 502


@app.post("/api/recognize-price-tag")
def recognize_price_tag():
    image = request.files.get("image")
    if image is None:
        return jsonify(error="사진 파일이 없습니다."), 400

    mime = image.mimetype or "image/jpeg"
    if mime not in ALLOWED_TYPES:
        return jsonify(error="JPG, PNG, WEBP만 지원합니다."), 400

    raw = image.read()
    data_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    prompt = """
한국 마트의 상품 매대 가격표 사진을 분석하라.
사진에 실제로 보이는 상품명, 판매가격, 행사정보, 용량/구성만 사용하고 추측하지 마라.
반드시 JSON 하나만 반환:
{
  "product_name":"",
  "price":0,
  "promotion":"",
  "volume":"",
  "confidence":"높음|보통|낮음"
}
"""

    try:
        response = get_client().responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            input=[{
                "role":"user",
                "content":[
                    {"type":"input_text","text":prompt},
                    {"type":"input_image","image_url":data_url}
                ]
            }],
            max_output_tokens=400
        )
        return jsonify(parse_json(response.output_text))
    except Exception as exc:
        return jsonify(error=f"가격표 인식 오류: {exc}"), 502


@app.errorhandler(404)
def not_found(_):
    return send_from_directory(BASE_DIR, "index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
