import base64,json,os,re
from flask import Flask,jsonify,request,send_from_directory
from openai import OpenAI
app=Flask(__name__,static_folder='.')
ALLOWED={'image/jpeg','image/png','image/webp'}
def cli():
    k=os.getenv('OPENAI_API_KEY')
    if not k: raise RuntimeError('OPENAI_API_KEY가 설정되지 않았습니다.')
    return OpenAI(api_key=k)
def parse(t):
    m=re.search(r'\{.*\}',(t or '').strip(),re.S)
    if not m: raise ValueError('JSON 응답을 찾지 못했습니다.')
    return json.loads(m.group(0))
def vision(prompt,tokens=500):
    f=request.files.get('image')
    if not f: return None,('사진 파일이 없습니다.',400)
    mime=f.mimetype or 'image/jpeg'
    if mime not in ALLOWED:return None,('JPG, PNG, WEBP만 지원합니다.',400)
    url=f'data:{mime};base64,{base64.b64encode(f.read()).decode()}'
    r=cli().responses.create(model=os.getenv('OPENAI_MODEL','gpt-4.1-mini'),input=[{'role':'user','content':[{'type':'input_text','text':prompt},{'type':'input_image','image_url':url}]}],max_output_tokens=tokens)
    return parse(r.output_text),None
@app.get('/')
def home():return send_from_directory('.','index.html')
@app.get('/<path:p>')
def static_file(p):return send_from_directory('.',p)
@app.post('/api/recognize-product')
def product():
    try:
        d,e=vision('상품 사진을 분석한다. 사진에 실제로 보이는 정보만 사용한다. JSON 하나만 반환: {"product_name":"","brand":"","volume":"","count":0,"confidence":"높음|보통|낮음"}')
        return (jsonify(error=e[0]),e[1]) if e else jsonify(d)
    except Exception as x:return jsonify(error=f'상품 인식 오류: {x}'),502
@app.post('/api/recognize-price-tag')
def price():
    try:
        d,e=vision('한국 매장 가격표를 분석한다. 실제로 보이는 정보만 사용한다. JSON 하나만 반환: {"product_name":"","price":0,"promotion":"","volume":"","confidence":"높음|보통|낮음"}')
        return (jsonify(error=e[0]),e[1]) if e else jsonify(d)
    except Exception as x:return jsonify(error=f'가격표 인식 오류: {x}'),502
@app.post('/api/recognize-receipt')
def receipt():
    try:
        d,e=vision('한국 마트 영수증을 분석한다. 실제로 확인되는 내용만 사용한다. JSON 하나만 반환: {"store":"","date":"","total":0,"items":[{"name":"","qty":1,"amount":0}],"confidence":"높음|보통|낮음"}',900)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(d)
    except Exception as x:return jsonify(error=f'영수증 인식 오류: {x}'),502
@app.post('/api/recognize-sneaker-label')
def sneaker():
    try:
        d,e=vision('신발 박스 라벨 또는 택을 분석한다. 나이키, 뉴발란스, 아디다스, 언더아머 중심이다. 보이는 정보만 사용하고 모델번호를 정확히 유지한다. 사이즈는 한국/JP mm를 우선한다. JSON 하나만 반환: {"brand":"나이키|뉴발란스|아디다스|언더아머|기타","model_no":"","product_name":"","size":0,"color":"","barcode":"","confidence":"높음|보통|낮음"}')
        return (jsonify(error=e[0]),e[1]) if e else jsonify(d)
    except Exception as x:return jsonify(error=f'신발 라벨 인식 오류: {x}'),502

@app.post('/api/recognize-kream-capture')
def kream_capture():
    try:
        d,e=vision('KREAM 상품 상세 또는 시세 화면 캡처를 분석한다. 사용자가 선택한 특정 사이즈 화면에서 보이는 정보만 정확히 추출한다. 보이지 않는 값은 0 또는 빈 문자열로 둔다. 날짜는 YYYY-MM-DD 형식으로 변환한다. 최근 거래 내역은 화면에 보이는 순서대로 최대 5건 반환한다. JSON 하나만 반환: {"model_no":"","size":0,"highest_bid":0,"lowest_ask":0,"recent_price":0,"trades":[{"date":"YYYY-MM-DD","price":0}],"confidence":"높음|보통|낮음"}',1000)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(d)
    except Exception as x:return jsonify(error=f'KREAM 캡처 인식 오류: {x}'),502

@app.post('/api/analyze-kream-url')
def analyze_kream_url():
    try:
        body=request.get_json(silent=True) or {}
        url=str(body.get('url','')).strip()
        wanted_size=int(body.get('size') or 0)
        if not re.match(r'^https://(?:www\.)?kream\.co\.kr/products/\d+(?:[/?#].*)?$',url,re.I):
            return jsonify(error='올바른 KREAM 상품 주소가 아닙니다.'),400
        prompt=f"""오늘 날짜는 2026-07-18이다. 다음 KREAM 상품 URL의 공개적으로 확인 가능한 정보를 웹 검색으로 조사한다.
URL: {url}
사용자가 관심 있는 사이즈: {wanted_size if wanted_size else '미지정'}

KREAM 페이지, 검색엔진에 노출된 KREAM 결과, 신뢰할 만한 공개 페이지에서 실제로 확인되는 내용만 사용한다. 추측하지 않는다.
상품명, 브랜드, 모델번호, 발매가, 사이즈별 최근 거래가격과 거래일, 공개된 최고 구매입찰가와 최저 판매입찰가, 확인 가능한 최근 거래 여러 건을 수집한다.
값이 공개되지 않으면 0 또는 빈 배열로 둔다. 가격은 원 단위 정수, 날짜는 YYYY-MM-DD 형식이다.
사이즈별 데이터는 확인 가능한 모든 사이즈를 반환하고, 같은 사이즈의 거래가 여러 개면 trades에 최신순으로 최대 10건 넣는다.
반드시 설명이나 마크다운 없이 JSON 하나만 반환한다.
{{"product_name":"","brand":"Nike|New Balance|Adidas|Under Armour|기타","model_no":"","color":"","release_price":0,"sizes":[{{"size":0,"highest_bid":0,"lowest_ask":0,"recent_price":0,"recent_date":"","trade_count":0,"trades":[{{"date":"YYYY-MM-DD","price":0}}]}}],"summary":"공개정보 기반 핵심 판단 한두 문장","confidence":"높음|보통|낮음"}}"""
        client=cli()
        last_error=None
        response=None
        for tool_type in ('web_search','web_search_preview'):
            try:
                response=client.responses.create(
                    model=os.getenv('OPENAI_WEB_MODEL',os.getenv('OPENAI_MODEL','gpt-4.1-mini')),
                    tools=[{'type':tool_type}],
                    input=prompt,
                    max_output_tokens=1800
                )
                break
            except Exception as exc:
                last_error=exc
        if response is None:
            raise last_error or RuntimeError('웹 검색 도구를 사용할 수 없습니다.')
        d=parse(response.output_text)
        d['checked_at']='2026-07-18'
        d['source_url']=url
        return jsonify(d)
    except Exception as x:
        return jsonify(error=f'KREAM 링크 분석 오류: {x}'),502

@app.get('/health')
def health():return jsonify(ok=True)
if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.getenv('PORT','10000')))
