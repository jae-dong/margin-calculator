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

def _clean_code(v):
    return re.sub(r'[^A-Z0-9-]', '', str(v or '').upper())

def _model_score(code, brand=''):
    c=_clean_code(code)
    if not c or len(c)<5 or len(c)>14:
        return -999
    # 바코드/일련번호로 보이는 긴 문자열은 강하게 제외
    if len(c)>=13:
        return -80
    if re.fullmatch(r'\d{8,}', c):
        return -100
    score=0
    patterns=[
        r'^(?:U|M|ML|MR|BB|CM|MS|MT|WR|WL|GC|GS)\d{3,4}[A-Z0-9]{1,5}$', # New Balance
        r'^(?:DD|DV|FD|DQ|CZ|DH|DR|FB|FN|HF|HJ|HV)\d{4}-?\d{3}$', # Nike
        r'^(?:IF|IG|ID|IE|IH|JI|JR|JS|GX|GY|HQ|HP|H0)\d{4}$', # Adidas
        r'^12(?:01|03)[A-Z]\d{3}-?\d{3}$', # ASICS
        r'^[A-Z]{1,3}\d{3,5}[A-Z0-9]{1,5}$' # generic sneaker model
    ]
    for i,pat in enumerate(patterns):
        if re.fullmatch(pat,c):
            score=max(score,100-i*8)
    if re.search(r'[A-Z]',c) and re.search(r'\d',c): score+=12
    if 7<=len(c)<=10: score+=15
    if '-' in c: score+=3
    # 라벨 내부관리번호/바코드 계열로 자주 보이는 패턴 감점
    if c.startswith(('NBPDFS','EAN','UPC','SKU')): score-=55
    if re.search(r'\d{5,}$',c) and len(c)>11: score-=45
    return score

def normalize_sneaker_result(d):
    d=dict(d or {})
    candidates=[]
    for key in ('model_no','style_code','article_no','product_code'):
        if d.get(key): candidates.append(d.get(key))
    for x in d.get('model_candidates') or []:
        if isinstance(x,dict): candidates.append(x.get('text') or x.get('code'))
        else: candidates.append(x)
    ranked=sorted(((_model_score(x,d.get('brand','')), _clean_code(x)) for x in candidates), reverse=True)
    best=ranked[0][1] if ranked and ranked[0][0]>0 else _clean_code(d.get('model_no'))
    d['model_no']=best
    d['barcode']=_clean_code(d.get('barcode'))
    d['internal_code']=_clean_code(d.get('internal_code'))
    d.pop('model_candidates',None)
    return d

def vision(prompt,tokens=500,multiple=False):
    files=request.files.getlist('images') if multiple else [request.files.get('image')]
    files=[f for f in files if f]
    if not files: return None,('사진 파일이 없습니다.',400)
    content=[{'type':'input_text','text':prompt}]
    for f in files[:6]:
        mime=f.mimetype or 'image/jpeg'
        if mime not in ALLOWED:return None,('JPG, PNG, WEBP만 지원합니다.',400)
        url=f'data:{mime};base64,{base64.b64encode(f.read()).decode()}'
        content.append({'type':'input_image','image_url':url})
    r=cli().responses.create(model=os.getenv('OPENAI_MODEL','gpt-4.1-mini'),input=[{'role':'user','content':content}],max_output_tokens=tokens)
    return parse(r.output_text),None
@app.get('/')
def home():return send_from_directory('.','index.html')
@app.get('/<path:p>')
def static_file(p):return send_from_directory('.',p)
GENERAL_PRODUCT_PROMPT = '''일반상품 소싱용 사진을 정밀 분석한다. 상품 본체, 포장 앞면/뒷면, 가격표, 신발 박스 라벨 중 하나일 수 있다. 사진에 실제로 보이는 정보만 사용하고 추측하지 않는다.

반드시 확인할 항목:
1. 브랜드와 정확한 상품명. 포장에 적힌 핵심 제품명, 라인명, 맛/향/색상/종류를 분리한다.
2. 용량·중량·규격·입수·묶음 수량. 예: 210g, 500ml, 30매, 6입.
3. 바코드 숫자(EAN/UPC/GTIN). 바코드 아래 숫자를 정확히 읽되 모델번호와 혼동하지 않는다.
4. 제조사 또는 수입자, 원산지, 제품 유형이 보이면 기록한다.
5. 디자인 식별정보: 포장 주색상, 로고 위치, 캐릭터, 용기 형태, 전면에 보이는 핵심 문구를 짧게 정리한다. 검색어 보조용일 뿐 보이지 않는 특징은 만들지 않는다.
6. 신발·의류·가전 등 모델번호가 있는 상품은 product_code에 정확히 넣는다. 신발은 브랜드 스타일코드(예: U9060ECA, DD1391-100, IF6490)를 바코드나 내부 일련번호보다 우선한다.
7. 신발이면 한국/JP 사이즈(mm), US 사이즈, 색상을 각각 구분한다.
8. 가격표가 보이면 이미 할인 적용되어 실제 결제할 표시가격을 price에 넣고, 정상가는 list_price에 넣는다. 가격표가 없으면 0으로 둔다.
9. 쿠팡 검색에 가장 적합한 짧고 정확한 검색어를 coupang_query에 만든다. 브랜드 + 상품명 + 모델번호(있을 때) + 용량/수량 순으로 구성하고, 광고문구·가격·바코드는 넣지 않는다. 상품명이 불명확할 때만 바코드를 fallback_query에 넣는다.

설명 없이 JSON 하나만 반환:
{"category":"식품|생활용품|뷰티|완구|반려동물|유아용품|의류|신발|가전|기타","brand":"","product_name":"","variant":"","product_code":"","barcode":"","manufacturer":"","origin":"","volume":"","count":0,"size_mm":0,"us_size":"","color":"","design_features":[""],"list_price":0,"price":0,"promotion":"","coupang_query":"","fallback_query":"","visible_text":[""],"confidence":"높음|보통|낮음","warnings":[""]}'''

@app.post('/api/recognize-product')
def product():
    try:
        d,e=vision(GENERAL_PRODUCT_PROMPT,1200)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(d)
    except Exception as x:return jsonify(error=f'상품 정밀 인식 오류: {x}'),502
@app.post('/api/recognize-price-tag')
def price():
    try:
        d,e=vision(GENERAL_PRODUCT_PROMPT + '\n이 사진은 가격표일 가능성이 높다. 실제 결제할 표시가격과 연결된 상품명·모델번호·바코드를 특히 정확히 읽는다.',1200)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(d)
    except Exception as x:return jsonify(error=f'가격표 정밀 인식 오류: {x}'),502
@app.post('/api/recognize-receipt')
def receipt():
    try:
        d,e=vision('한국 마트 영수증을 분석한다. 실제로 확인되는 내용만 사용한다. JSON 하나만 반환: {"store":"","date":"","total":0,"items":[{"name":"","qty":1,"amount":0}],"confidence":"높음|보통|낮음"}',900)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(d)
    except Exception as x:return jsonify(error=f'영수증 인식 오류: {x}'),502
@app.post('/api/recognize-sneaker-label')
def sneaker():
    try:
        prompt='''신발 박스 라벨 또는 택을 분석한다. 화면에 실제 보이는 글자만 사용한다. 가장 중요한 작업은 모델번호와 사이즈를 정확히 구분하는 것이다.

모델번호 선택 규칙:
1. 브랜드의 실제 스타일코드 형태를 최우선으로 선택한다. 예: 뉴발란스 U9060ECA, ML725R, M2002RCC, BB550WWW / 나이키 DD1391-100, DV0833-100 / 아디다스 IF6490, IG6199.
2. 바코드 바로 아래의 긴 문자열, EAN/UPC, 내부 물류번호, 일련번호는 모델번호로 선택하지 않는다.
3. 같은 사진에 'NBPDFS193I', 'U9060ECA', 'NBPDFS193I39240'가 함께 있으면 모델번호는 반드시 U9060ECA이며, NBPDFS193I는 internal_code, NBPDFS193I39240는 barcode다.
4. 한국/JP mm 사이즈를 우선한다. 큰 숫자 220~320 범위가 보이면 size에 넣고, US 6 같은 해외 사이즈와 혼동하지 않는다.
5. OCR 문자 I/1, O/0를 임의로 바꾸지 말고 라벨 글자를 그대로 유지한다.
6. 모델번호 후보를 위치와 함께 model_candidates에 모두 반환한다. 바코드 아래 후보는 role을 barcode_text로 표시한다.

설명 없이 JSON 하나만 반환:
{"brand":"나이키|뉴발란스|아디다스|언더아머|아식스|기타","model_no":"","model_candidates":[{"text":"","role":"model|internal|barcode_text|other"}],"internal_code":"","product_name":"","size":0,"us_size":"","color":"","barcode":"","confidence":"높음|보통|낮음"}'''
        d,e=vision(prompt,900)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(normalize_sneaker_result(d))
    except Exception as x:return jsonify(error=f'신발 라벨 인식 오류: {x}'),502


@app.post('/api/recognize-sneaker-batch')
def sneaker_batch():
    try:
        prompt='여러 장의 사진을 하나의 스니커즈 소싱 건으로 통합 분석한다. 사진들은 신발 박스 라벨, 아울렛 가격표, KREAM 체결 거래, 판매입찰, 구매입찰 화면이 섞여 있을 수 있다. 먼저 각 사진 유형을 분류한 뒤 같은 상품·같은 사이즈의 정보만 합친다. 실제 화면에 보이는 값만 사용하고 추측하지 않는다.\n\n모델번호 규칙: 브랜드 스타일코드를 최우선으로 선택한다. 바코드 아래 긴 문자열·EAN·UPC·내부 물류번호는 모델번호가 아니다. NBPDFS193I / U9060ECA / NBPDFS193I39240가 함께 있으면 model_no는 U9060ECA, internal_code는 NBPDFS193I, barcode는 NBPDFS193I39240이다.\n사이즈 규칙: 한국/JP mm 220~320을 우선하고 US 사이즈와 혼동하지 않는다.\n가격표 규칙: 가격표에 이미 할인 적용되어 크게 표시된 현재 판매가를 sale_price에 넣는다. 정상가는 list_price다. 가격표의 기존 할인율은 shown_discount_rate이며 사용자의 추가 할인율과 합산하지 않는다.\nKREAM 규칙: 실제 체결 거래만 trades에 넣고 날짜는 YYYY-MM-DD, 가격은 원 단위 정수로 한다. 판매입찰은 lowest_ask, 구매입찰은 highest_bid로 분리한다. 중복 체결은 제거하고 최신순 최대 10건으로 반환한다.\n서로 다른 모델이 섞이면 가장 많은 사진에서 일치하는 모델을 대표로 선택하고 conflicts에 경고를 넣는다.\n설명 없이 JSON 하나만 반환:\n{"image_types":["박스라벨","가격표","체결거래","판매입찰","구매입찰"],"brand":"나이키|뉴발란스|아디다스|언더아머|아식스|기타","model_no":"","model_candidates":[],"internal_code":"","barcode":"","product_name":"","color":"","size":0,"us_size":"","list_price":0,"sale_price":0,"shown_discount_rate":0,"highest_bid":0,"lowest_ask":0,"recent_price":0,"trades":[{"date":"YYYY-MM-DD","price":0}],"visible_trade_count":0,"conflicts":[],"confidence":"높음|보통|낮음"}'
        d,e=vision(prompt,2200,multiple=True)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(normalize_sneaker_result(d))
    except Exception as x:return jsonify(error=f'통합 사진 분석 오류: {x}'),502

@app.post('/api/recognize-kream-captures')
def kream_captures():
    try:
        prompt='''여러 장의 KREAM 화면 캡처를 하나의 묶음으로 분석한다. 핵심은 같은 상품·같은 사이즈의 체결 거래 내역이다. 실제 화면에 보이는 값만 사용하고 추측하지 않는다. 사이즈는 한국/JP mm를 우선한다. 날짜는 YYYY-MM-DD로 변환한다. 중복 거래는 제거하고 최신순 최대 10건을 반환한다. 판매입찰이나 구매입찰 화면이 함께 있어도 참고정보로만 구분하고, 일반판매 수익 계산에 사용할 가격은 trades의 실제 체결 거래가다. 최근 거래가는 가장 최신 체결가이다. 화면에 보이는 체결 거래 개수도 visible_trade_count에 넣는다. JSON 하나만 반환: {"model_no":"","product_name":"","size":0,"highest_bid":0,"lowest_ask":0,"recent_price":0,"trades":[{"date":"YYYY-MM-DD","price":0}],"visible_trade_count":0,"capture_types":["체결거래","판매입찰","구매입찰"],"confidence":"높음|보통|낮음"}'''
        d,e=vision(prompt,1600,multiple=True)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(d)
    except Exception as x:return jsonify(error=f'KREAM 다중 캡처 인식 오류: {x}'),502

@app.post('/api/recognize-sneaker-outlet-tag')
def sneaker_outlet_tag():
    try:
        prompt='''아울렛 신발 가격표 또는 신발 박스 라벨 사진을 분석한다. 모델번호는 브랜드 스타일코드 형식을 우선하고 바코드 아래의 긴 문자열·일련번호를 모델번호로 선택하지 않는다. 예를 들어 NBPDFS193I / U9060ECA / NBPDFS193I39240가 함께 있으면 model_no는 U9060ECA, internal_code는 NBPDFS193I, barcode는 NBPDFS193I39240이다. 사진에 함께 보이는 브랜드, 모델번호, 상품명, 색상, 한국/JP 사이즈(mm), 바코드, 정상가, 가격표에 이미 할인이 적용되어 표시된 현재 판매가, 가격표의 1차 할인율을 추출한다. 가장 중요한 값은 고객이 매장에서 추가 할인을 받기 전 가격표에 적힌 할인 적용 판매가이며 반드시 sale_price에 넣는다. 정상가와 할인가가 모두 보이면 정상가는 list_price, 이미 할인 적용된 표시가는 sale_price로 정확히 구분한다. 취소선 가격·권장소비자가·정상가는 sale_price로 넣지 않는다. 여러 가격이 있으면 'SALE', '할인가', '회원가', '판매가', 가장 크거나 강조된 결제 가격 등의 문맥으로 실제 표시 할인가를 판단한다. 가격표에 적힌 할인율은 shown_discount_rate이며 이것은 이미 sale_price에 반영된 1차 할인율이다. 사용자가 별도로 적용할 추가 할인율과 혼동하거나 합산하지 않는다. 한 가격만 보여 할인가인지 확실하지 않으면 price_type을 unknown으로 하고 확인된 가격을 list_price에 넣는다. 보이지 않는 값은 0 또는 빈 문자열로 둔다. 임의 추측 금지. JSON 하나만 반환: {"brand":"나이키|뉴발란스|아디다스|언더아머|아식스|기타","model_no":"","model_candidates":[{"text":"","role":"model|internal|barcode_text|other"}],"internal_code":"","product_name":"","size":0,"color":"","barcode":"","list_price":0,"sale_price":0,"shown_discount_rate":0,"price_type":"normal|sale|unknown","confidence":"높음|보통|낮음"}'''
        d,e=vision(prompt,900)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(normalize_sneaker_result(d))
    except Exception as x:return jsonify(error=f'아울렛 가격표 인식 오류: {x}'),502

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
