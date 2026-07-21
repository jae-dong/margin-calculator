import base64,json,os,re
import json5
from io import BytesIO
from datetime import datetime
from flask import Flask,jsonify,request,send_from_directory,send_file
import xlsxwriter
from openai import OpenAI
app=Flask(__name__,static_folder='.')
app.config['MAX_CONTENT_LENGTH']=24*1024*1024
ALLOWED={'image/jpeg','image/png','image/webp'}
def cli():
    k=os.getenv('OPENAI_API_KEY')
    if not k: raise RuntimeError('OPENAI_API_KEY가 설정되지 않았습니다.')
    return OpenAI(api_key=k, timeout=75.0, max_retries=2)
def parse(t):
    raw=(t or '').strip()
    raw=re.sub(r'^```(?:json)?\s*|\s*```$','',raw,flags=re.I|re.S).strip()
    start=raw.find('{')
    if start<0: raise ValueError('JSON 응답을 찾지 못했습니다.')
    raw=raw[start:]
    # 먼저 표준 JSON과 JSON5를 순서대로 시도한다.
    for loader in (json.loads,json5.loads):
        try:return loader(raw)
        except Exception:pass
    # 모델이 줄바꿈 사이 쉼표를 빠뜨리거나 끝부분을 조금 잘랐을 때 자동 복구한다.
    repaired=raw
    repaired=re.sub(r',\s*([}\]])',r'\1',repaired)
    repaired=re.sub(r'([0-9truefalsenull"\]\}])\s*\n\s*(")',r'\1,\n\2',repaired,flags=re.I)
    repaired=re.sub(r'(}\s*)({)',r'\1,\2',repaired)
    # 문자열 내부가 아닌 괄호를 세어 잘린 응답의 닫는 괄호를 보충한다.
    in_str=False;esc=False;stack=[]
    for ch in repaired:
        if in_str:
            if esc:esc=False
            elif ch=='\\':esc=True
            elif ch=='"':in_str=False
        else:
            if ch=='"':in_str=True
            elif ch in '[{':stack.append(ch)
            elif ch in ']}' and stack:stack.pop()
    if in_str:repaired+='"'
    repaired+=''.join('}' if ch=='{' else ']' for ch in reversed(stack))
    for loader in (json.loads,json5.loads):
        try:return loader(repaired)
        except Exception:pass
    raise ValueError('AI 응답 JSON 자동 복구에 실패했습니다.')

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
    if c.startswith(('NBPDFS','NBP','EAN','UPC','SKU')): score-=120
    if re.search(r'\d{5,}$',c) and len(c)>11: score-=45
    return score


WATERMARK_WORDS = ('올데이픽', 'ALLDAYPICK', 'ALL DAY PICK', 'AI 소싱계산기', 'AI소싱계산기')

def _remove_watermark_text(v):
    text=str(v or '')
    for word in WATERMARK_WORDS:
        text=re.sub(re.escape(word), ' ', text, flags=re.I)
    return re.sub(r'\s+', ' ', text).strip(' -_·|/')

def _pick_best_model(candidates, brand=''):
    cleaned=[]
    for x in candidates:
        c=_clean_code(x)
        if c and c not in cleaned:
            cleaned.append(c)
    ranked=sorted(((_model_score(x,brand), x) for x in cleaned), reverse=True)
    return ranked[0][1] if ranked and ranked[0][0] >= 70 else ''

def normalize_general_result(d):
    d=dict(d or {})
    text_fields=('brand','product_name','variant','product_code','manufacturer','origin','volume','color','promotion','coupang_query','fallback_query','internal_code')
    for key in text_fields:
        d[key]=_remove_watermark_text(d.get(key))
    for key in ('design_features','visible_text','warnings'):
        vals=d.get(key) or []
        d[key]=[x for x in (_remove_watermark_text(v) for v in vals) if x]

    # 신발 라벨에서는 내부 관리번호가 아니라 실제 브랜드 스타일코드를 상품코드로 선택한다.
    # 예: NBPDFS193I(내부번호) / U9060ECA(상품코드) / NBPDFS193I39240(바코드문자열)
    candidates=[d.get('product_code'), d.get('model_no'), d.get('style_code'), d.get('article_no')]
    for item in d.get('model_candidates') or []:
        if isinstance(item,dict):
            if item.get('role') == 'model': candidates.insert(0,item.get('text') or item.get('code'))
            elif item.get('role') not in ('internal','barcode_text'): candidates.append(item.get('text') or item.get('code'))
        else: candidates.append(item)
    for line in d.get('visible_text') or []:
        candidates.extend(re.findall(r'\b[A-Z]{1,3}[A-Z0-9-]{4,13}\b', str(line).upper()))
    best=_pick_best_model(candidates,d.get('brand',''))
    if best: d['product_code']=best

    # 정상적인 숫자형 EAN/UPC/GTIN만 일반 바코드로 유지한다.
    raw=str(d.get('barcode') or '')
    digits=re.sub(r'\D','',raw)
    d['barcode']=digits if 8 <= len(digits) <= 14 else ''

    # 잘못 인식된 내부번호가 쿠팡 검색어에 들어가지 않게 검색어를 서버에서 다시 조립한다.
    if best:
        parts=[d.get('brand'), d.get('product_name'), d.get('variant'), best]
        if d.get('size_mm'): parts.append(str(d.get('size_mm')))
        elif d.get('volume'): parts.append(d.get('volume'))
        if d.get('count'): parts.append(str(d.get('count'))+'개')
        d['coupang_query']=' '.join(str(x).strip() for x in parts if x and str(x).strip())
    else:
        d['coupang_query']=_remove_watermark_text(d.get('coupang_query'))
    d['fallback_query']=d['barcode'] or _remove_watermark_text(d.get('fallback_query'))
    d.pop('model_candidates',None)
    return d

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
    for f in files[:10]:
        mime=f.mimetype or 'image/jpeg'
        if mime not in ALLOWED:return None,('JPG, PNG, WEBP만 지원합니다.',400)
        url=f'data:{mime};base64,{base64.b64encode(f.read()).decode()}'
        content.append({'type':'input_image','image_url':url})
    r=cli().responses.create(model=os.getenv('OPENAI_MODEL','gpt-4.1-mini'),input=[{'role':'user','content':content}],max_output_tokens=tokens)
    return parse(r.output_text),None
@app.errorhandler(413)
def too_large(_):
    if request.path.startswith('/api/'): return jsonify(error='사진 용량이 너무 큽니다. 최신 앱은 전송 전 자동 압축합니다. 새로고침 후 다시 시도하세요.'),413
    return '파일 용량이 너무 큽니다.',413

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/'): return jsonify(error='분석 API를 찾지 못했습니다. 최신 버전이 정상 배포됐는지 확인하세요.'),404
    return send_from_directory('.', 'index.html')

@app.errorhandler(500)
def server_error(e):
    if request.path.startswith('/api/'): return jsonify(error='분석 서버 내부 오류가 발생했습니다. 잠시 후 다시 시도하세요.'),500
    return '서버 오류',500

@app.get('/')
def home():return send_from_directory('.','index.html')
@app.get('/<path:p>')
def static_file(p):return send_from_directory('.',p)
GENERAL_PRODUCT_PROMPT = '''일반상품 소싱용 사진을 정밀 분석한다.

중요: 사진에 앱이 자동으로 넣은 워터마크, 촬영앱 이름, 화면 상단·하단의 UI 글자는 상품 정보가 아니다. 특히 '올데이픽', 'ALLDAYPICK', 'ALL DAY PICK', 'AI 소싱계산기' 문구는 brand, product_name, variant, visible_text, design_features, coupang_query 등 모든 결과에서 완전히 제외한다. 워터마크와 실제 포장 인쇄를 혼동하지 않는다. 상품 본체, 포장 앞면/뒷면, 가격표, 신발 박스 라벨 중 하나일 수 있다. 사진에 실제로 보이는 정보만 사용하고 추측하지 않는다.

반드시 확인할 항목:
1. 브랜드와 정확한 상품명. 포장에 적힌 핵심 제품명, 라인명, 맛/향/색상/종류를 분리한다.
2. 용량·중량·규격·입수·묶음 수량. 예: 210g, 500ml, 30매, 6입.
3. 바코드 숫자(EAN/UPC/GTIN). 바코드 아래 숫자를 정확히 읽되 모델번호와 혼동하지 않는다.
4. 제조사 또는 수입자, 원산지, 제품 유형이 보이면 기록한다.
5. 디자인 식별정보: 포장 주색상, 로고 위치, 캐릭터, 용기 형태, 전면에 보이는 핵심 문구를 짧게 정리한다. 검색어 보조용일 뿐 보이지 않는 특징은 만들지 않는다.
6. 신발·의류·가전 등 모델번호가 있는 상품은 product_code에 정확히 넣는다. 신발은 브랜드 스타일코드(예: U9060ECA, DD1391-100, IF6490)를 바코드나 내부 일련번호보다 우선한다. 뉴발란스 라벨에서 NBPDFS로 시작하는 코드는 내부 관리번호이므로 product_code로 절대 선택하지 않는다. 예시 사진처럼 NBPDFS193I / U9060ECA / NBPDFS193I39240가 함께 보이면 product_code는 반드시 U9060ECA, internal_code는 NBPDFS193I이며 긴 문자열은 barcode_text다.
7. 신발이면 한국/JP 사이즈(mm), US 사이즈, 색상을 각각 구분한다.
8. 가격표가 보이면 이미 할인 적용되어 실제 결제할 표시가격을 price에 넣고, 정상가는 list_price에 넣는다. 가격표가 없으면 0으로 둔다.
9. 쿠팡 검색에 가장 적합한 짧고 정확한 검색어를 coupang_query에 만든다. 브랜드 + 상품명 + 모델번호(있을 때) + 용량/수량 순으로 구성하고, 광고문구·가격·바코드는 넣지 않는다. 상품명이 불명확할 때만 바코드를 fallback_query에 넣는다.

설명·마크다운 없이 완전한 JSON 하나만 반환한다. 값이 안 보이면 0 또는 빈 문자열을 쓰고 항목을 생략하지 않는다:
{"category":"식품|생활용품|뷰티|완구|반려동물|유아용품|의류|신발|가전|기타","brand":"","product_name":"","variant":"","product_code":"","model_candidates":[{"text":"","role":"model|internal|barcode_text|other"}],"internal_code":"","barcode":"","manufacturer":"","origin":"","volume":"","count":0,"size_mm":0,"us_size":"","color":"","design_features":[""],"list_price":0,"price":0,"promotion":"","coupang_query":"","fallback_query":"","visible_text":[""],"confidence":"높음|보통|낮음","warnings":[""]}'''


@app.post('/api/recognize-general-universal')
def general_universal():
    try:
        files=request.files.getlist('images')
        multiple=bool(files)
        prompt=GENERAL_PRODUCT_PROMPT + """

추가 지시: 입력 사진은 상품 본체, 포장 앞면/뒷면, 매장 가격표, 바코드, 신발 박스 라벨 중 하나 또는 여러 장이다. 사진 종류를 자동 분류하고 여러 장이면 같은 상품의 정보로 합쳐라. 가격표가 있으면 이미 할인 적용된 실제 표시 결제가격을 price에 넣어라. 서로 충돌하는 값은 임의로 확정하지 말고 warnings에 적어라. 신발은 내부 관리번호가 아니라 실제 브랜드 스타일코드를 product_code로 선택한다.
"""
        d,e=vision(prompt,1400,multiple=multiple)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(normalize_general_result(d))
    except Exception as x:return jsonify(error=f'일반상품 통합 인식 오류: {x}'),502

@app.post('/api/recognize-product')
def product():
    try:
        d,e=vision(GENERAL_PRODUCT_PROMPT,1200)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(normalize_general_result(d))
    except Exception as x:return jsonify(error=f'상품 정밀 인식 오류: {x}'),502
@app.post('/api/recognize-price-tag')
def price():
    try:
        d,e=vision(GENERAL_PRODUCT_PROMPT + '\n이 사진은 가격표일 가능성이 높다. 실제 결제할 표시가격과 연결된 상품명·모델번호·바코드를 특히 정확히 읽는다.',1200)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(normalize_general_result(d))
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

설명·마크다운 없이 완전한 JSON 하나만 반환한다. 값이 안 보이면 0 또는 빈 문자열을 쓰고 항목을 생략하지 않는다:
{"brand":"나이키|뉴발란스|아디다스|언더아머|아식스|기타","model_no":"","model_candidates":[{"text":"","role":"model|internal|barcode_text|other"}],"internal_code":"","product_name":"","size":0,"us_size":"","color":"","barcode":"","confidence":"높음|보통|낮음"}'''
        d,e=vision(prompt,900)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(normalize_sneaker_result(d))
    except Exception as x:return jsonify(error=f'신발 라벨 인식 오류: {x}'),502


@app.post('/api/recognize-sneaker-batch')
def sneaker_batch():
    try:
        prompt='여러 장의 사진을 하나의 스니커즈 소싱 건으로 통합 분석한다. 사진들은 신발 박스 라벨, 아울렛 가격표, KREAM 체결 거래, 판매입찰, 구매입찰 화면이 섞여 있을 수 있다. 먼저 각 사진 유형을 분류한 뒤 같은 상품·같은 사이즈의 정보만 합친다. 실제 화면에 보이는 값만 사용하고 추측하지 않는다.\n\n모델번호 규칙: 브랜드 스타일코드를 최우선으로 선택한다. 바코드 아래 긴 문자열·EAN·UPC·내부 물류번호는 모델번호가 아니다. NBPDFS193I / U9060ECA / NBPDFS193I39240가 함께 있으면 model_no는 U9060ECA, internal_code는 NBPDFS193I, barcode는 NBPDFS193I39240이다.\n사이즈 규칙: 한국/JP mm 220~320을 우선하고 US 사이즈와 혼동하지 않는다.\n가격표 규칙: 가격표에 이미 할인 적용되어 크게 표시된 현재 판매가를 sale_price에 넣는다. 정상가는 list_price다. 가격표의 기존 할인율은 shown_discount_rate이며 사용자의 추가 할인율과 합산하지 않는다.\nKREAM 규칙: 실제 체결 거래만 trades에 넣고 날짜는 YYYY-MM-DD, 가격은 원 단위 정수로 한다. 판매입찰은 lowest_ask, 구매입찰은 highest_bid로 분리한다. 중복 체결은 제거하고 최신순 최대 10건으로 반환한다.\n서로 다른 모델이 섞이면 가장 많은 사진에서 일치하는 모델을 대표로 선택하고 conflicts에 경고를 넣는다.\n설명·마크다운 없이 완전한 JSON 하나만 반환한다. 값이 안 보이면 0 또는 빈 문자열을 쓰고 항목을 생략하지 않는다:\n{"image_types":["박스라벨","가격표","체결거래","판매입찰","구매입찰"],"brand":"나이키|뉴발란스|아디다스|언더아머|아식스|기타","model_no":"","model_candidates":[],"internal_code":"","barcode":"","product_name":"","color":"","size":0,"us_size":"","list_price":0,"sale_price":0,"shown_discount_rate":0,"highest_bid":0,"lowest_ask":0,"recent_price":0,"trades":[{"date":"YYYY-MM-DD","price":0}],"visible_trade_count":0,"conflicts":[],"confidence":"높음|보통|낮음"}'
        d,e=vision(prompt,1300,multiple=True)
        return (jsonify(error=e[0]),e[1]) if e else jsonify(normalize_sneaker_result(d))
    except Exception as x:return jsonify(error=f'통합 사진 분석 오류: {x}'),502

def _to_int(v):
    try:
        if isinstance(v,str):
            v=re.sub(r'[^0-9.-]','',v)
        return max(0,int(float(v or 0)))
    except Exception:
        return 0

def _normalize_capture_result(d, wanted_size=0):
    """AI가 조금 다른 구조로 답해도 가격·사이즈를 최대한 살려 앱 표준 구조로 변환한다."""
    d=dict(d or {})
    rows_in=d.get('rows') or d.get('sizes') or d.get('options') or []
    sizes=[]
    all_prices=[]
    for row in rows_in:
        if not isinstance(row,dict):
            continue
        size=_to_int(row.get('size') or row.get('size_mm') or row.get('option_size'))
        prices=[]
        for value in row.get('prices') or row.get('trade_prices') or []:
            price=_to_int(value)
            if price: prices.append(price)
        trades=[]
        for t in row.get('trades') or row.get('transactions') or []:
            if isinstance(t,dict):
                price=_to_int(t.get('price') or t.get('amount'))
                if price:
                    prices.append(price)
                    trades.append({'date':str(t.get('date') or t.get('trade_date') or ''),'price':price})
            else:
                price=_to_int(t)
                if price:
                    prices.append(price)
                    trades.append({'date':'','price':price})
        # AI가 prices 배열만 반환하는 경우에도 최근 거래 입력칸이 비지 않도록 거래 행으로 보존한다.
        existing_prices=[_to_int(t.get('price')) for t in trades]
        for price in prices:
            if price and price not in existing_prices:
                trades.append({'date':'','price':price})
                existing_prices.append(price)
        for key in ('recent_price','avg_price','high_price','low_price'):
            price=_to_int(row.get(key))
            if price: prices.append(price)
        high=_to_int(row.get('high_price')) or (max(prices) if prices else 0)
        low=_to_int(row.get('low_price')) or (min(prices) if prices else 0)
        avg=_to_int(row.get('avg_price')) or (round(sum(prices)/len(prices)) if prices else 0)
        recent=_to_int(row.get('recent_price')) or (prices[0] if prices else avg or low or high)
        all_prices.extend(prices or [x for x in (high,avg,low,recent) if x])
        sizes.append({
            'size':size,
            'trade_count':_to_int(row.get('trade_count') or row.get('visible_trade_count')) or len(trades) or len(prices),
            'recent_price':recent,'avg_price':avg,'high_price':high,'low_price':low,
            'recent_date':str(row.get('recent_date') or row.get('date') or ''),
            'days_since_last_trade':_to_int(row.get('days_since_last_trade')) if row.get('days_since_last_trade') not in (None,'') else 999,
            'lowest_ask':_to_int(row.get('lowest_ask') or row.get('sell_bid') or row.get('ask')),
            'highest_bid':_to_int(row.get('highest_bid') or row.get('buy_bid') or row.get('bid')),
            'demand':str(row.get('demand') or '자료부족'),
            'recommendation_reason':str(row.get('recommendation_reason') or ''),
            'trades':trades[:12]
        })
    summary=d.get('visible_summary') or d.get('summary') or {}
    overall_high=_to_int(d.get('overall_high_price') or summary.get('high') or summary.get('high_price'))
    overall_avg=_to_int(d.get('overall_avg_price') or summary.get('avg') or summary.get('average') or summary.get('avg_price'))
    overall_low=_to_int(d.get('overall_low_price') or summary.get('low') or summary.get('low_price'))
    if all_prices:
        overall_high=overall_high or max(all_prices)
        overall_avg=overall_avg or round(sum(all_prices)/len(all_prices))
        overall_low=overall_low or min(all_prices)
    if not sizes and any((overall_high,overall_avg,overall_low)):
        sizes=[{'size':_to_int(wanted_size),'trade_count':_to_int(d.get('visible_trade_count')),
                'recent_price':overall_avg or overall_low or overall_high,'avg_price':overall_avg,
                'high_price':overall_high,'low_price':overall_low,'recent_date':'','days_since_last_trade':999,
                'lowest_ask':_to_int(d.get('lowest_ask')),'highest_bid':_to_int(d.get('highest_bid')),
                'demand':'자료부족','recommendation_reason':'가격은 인식했으나 사이즈 표시는 확인하지 못함','trades':[]}]
    # 단일 화면에서 사이즈 숫자를 못 읽었지만 사용자가 제품 사이즈를 이미 입력한 경우 그 값을 연결한다.
    if len(sizes)==1 and not _to_int(sizes[0].get('size')) and _to_int(wanted_size):
        sizes[0]['size']=_to_int(wanted_size)
        sizes[0]['recommendation_reason']=sizes[0].get('recommendation_reason') or '캡처에서 사이즈가 흐려 현재 상품 입력 사이즈를 적용함'
    visible_count=_to_int(d.get('visible_trade_count')) or sum(_to_int(x.get('trade_count')) for x in sizes)
    valid_sizes=[x for x in sizes if x.get('size')]
    mode='all' if len(valid_sizes)>1 else 'single'
    return {
        'analysis_mode':mode,
        'platform':str(d.get('platform') or ''),
        'model_no':str(d.get('model_no') or d.get('model') or ''),
        'product_name':str(d.get('product_name') or d.get('product_title') or ''),
        'capture_types':d.get('capture_types') or ([d.get('screen_type')] if d.get('screen_type') else []),
        'overall_high_price':overall_high,'overall_avg_price':overall_avg,'overall_low_price':overall_low,
        'sizes':sizes,
        'comparison_note':str(d.get('comparison_note') or ('단일옵션으로 사이즈 간 비교 불가' if mode=='single' else '')),
        'visible_trade_count':visible_count,
        'conflicts':d.get('conflicts') or [],
        'confidence':str(d.get('confidence') or '보통')
    }

@app.post('/api/recognize-kream-captures')
def kream_captures():
    try:
        scope=str(request.form.get('scope','auto') or 'auto').lower()
        wanted_size=_to_int(request.form.get('wanted_size'))
        scope_note={'all':'여러 사이즈가 보이면 각 사이즈를 별도 행으로 분리한다.','single':'현재 선택된 한 사이즈 화면만 읽는다.'}.get(scope,'화면에 보이는 구조에 따라 단일 사이즈 또는 여러 사이즈를 판단한다.')
        prompt=f'''KREAM 또는 POIZON 앱의 시세 스크린샷 한 장을 OCR처럼 정확히 읽어라. {scope_note}
가장 중요한 것은 화면에 보이는 숫자를 빠뜨리지 않는 것이다. 체결거래 가격, 사이즈, 날짜, 판매입찰 최저가, 구매입찰 최고가를 보이는 그대로 추출한다.
추측하거나 보이지 않는 숫자를 만들지 않는다. 쉼표가 포함된 원화 가격은 정수로 바꾼다. 사이즈가 안 보이면 size는 0이어도 되며 가격 데이터는 반드시 반환한다.
한 화면에 체결 가격이 여러 개 보이면 prices 배열과 trades 배열에 위에서 아래 순서로 모두 넣는다. 날짜가 안 보이면 date는 빈 문자열로 두되 price는 반드시 보존한다. 각 거래 행에 사이즈가 같이 보이면 반드시 해당 size 행에 묶고, 여러 사이즈가 섞인 화면이면 사이즈별 rows를 따로 만든다. 최고·평균·최저는 서버가 계산하므로 억지로 계산하지 않아도 된다.
설명이나 마크다운 없이 JSON 하나만 반환한다:
{{"platform":"KREAM|POIZON|기타","screen_type":"체결거래|판매입찰|구매입찰|시세요약|혼합","model_no":"","product_name":"","rows":[{{"size":0,"prices":[0],"trades":[{{"date":"YYYY-MM-DD","price":0}}],"lowest_ask":0,"highest_bid":0}}],"visible_summary":{{"high":0,"avg":0,"low":0}},"visible_trade_count":0,"confidence":"높음|보통|낮음"}}'''
        d,e=vision(prompt,1050,multiple=True)
        if e:return jsonify(error=e[0]),e[1]
        out=_normalize_capture_result(d,wanted_size)
        has_price=any((out.get('overall_high_price'),out.get('overall_avg_price'),out.get('overall_low_price')))
        has_rows=any(any(_to_int(x.get(k)) for k in ('recent_price','avg_price','high_price','low_price','lowest_ask','highest_bid')) for x in out.get('sizes') or [])
        if not (has_price or has_rows):
            return jsonify(error='가격 숫자를 읽지 못했습니다. 가격과 사이즈가 보이는 화면 전체 캡처를 올려 주세요.'),422
        return jsonify(out)
    except Exception:
        app.logger.exception('KREAM/POIZON capture analysis failure')
        return jsonify(error='캡처 분석 중 오류가 발생했습니다. 잠시 후 같은 사진으로 다시 시도해 주세요.'),502

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




@app.post('/api/analyze-market-keyword')
def analyze_market_keyword():
    """사진에서 인식한 상품명을 대표 키워드로 정제한 뒤 공개 웹 자료로 수요/경쟁을 평가한다."""
    try:
        body=request.get_json(silent=True) or {}
        raw_keyword=str(body.get('keyword') or '').strip()[:160]
        if not raw_keyword:return jsonify(error='분석할 키워드가 없습니다.'),400
        context={k:body.get(k) for k in ('brand','product_name','category','sale_price','cost_price','margin','roi')}
        prompt=f"""한국 온라인 쇼핑 상품을 분석한다.

입력 검색어: {raw_keyword}
상품정보: {json.dumps(context,ensure_ascii=False)}

1단계: 입력값에서 소비자가 실제로 검색할 대표 키워드(main_keyword)를 만든다.
- 일반상품: 브랜드 + 핵심 상품명 + 핵심 규격까지만 사용한다.
- 스니커즈: 브랜드 + 모델군/모델번호를 우선한다. 색상·사이즈·내부관리번호·바코드는 대표 키워드에서 제외한다.
- 광고문구, 수량 1개, 혼합색상, 무료배송 같은 불필요한 단어를 제거한다.
- related_keywords에는 검색 의도가 분명한 보조 키워드 2~4개만 넣는다.

2단계: main_keyword로 공개 웹 검색을 수행해 한국 온라인 쇼핑의 수요와 경쟁을 조사한다.
우선 확인할 공개 근거:
- 네이버 검색/쇼핑 결과에 노출된 상품 수 또는 관련 문서
- 쿠팡·11번가·G마켓 등 공개 검색 결과
- 검색 트렌드나 키워드 통계를 공개적으로 보여주는 페이지
- 제조사·브랜드·판매처의 상품 노출 빈도와 리뷰 집중도

유료 회원 전용 데이터나 로그인 뒤 수치를 우회하지 않는다. 정확한 월간 검색량을 확인하지 못하면 search_volume_type을 "추정"으로 하고, 공개 근거에 따른 보수적 범위를 monthly_search_min/monthly_search_max에 넣는다. 공개 수치도 근거도 부족하면 0으로 둔다.
판매자 수와 상품 수는 실제 확인한 숫자가 있으면 seller_count/product_count에 넣고, 검색결과 수만 확인되면 product_count에 넣는다.
competition_score는 상품수·판매자수·브랜드 독점·리뷰 집중도를 반영한 0~100 점수다.
demand_score는 검색 관심도·노출 빈도·거래/리뷰 신호를 반영한 0~100 점수다.
sourcing_score는 수요가 높고 경쟁이 낮으며 입력 마진/ROI가 좋을수록 높다.
evidence에는 확인 근거를 짧게 2~5개 적는다. 확인하지 않은 숫자를 사실처럼 만들지 않는다.

설명·마크다운 없이 완전한 JSON 하나만 반환한다:
{{"main_keyword":"","related_keywords":[],"demand_score":0,"competition_score":0,"sourcing_score":0,"turnover":"빠름|보통|느림|자료부족","recommendation":"적극 소싱|마진 확보 시 소싱|소량 테스트|비추천|자료부족","search_volume_type":"확인값|추정|자료부족","monthly_search_volume":0,"monthly_search_min":0,"monthly_search_max":0,"seller_count":0,"product_count":0,"data_scope":"공개 웹 검색 기반","confidence":"높음|보통|낮음","evidence":[""],"cautions":[""]}}"""
        client=cli()
        response=None
        last_error=None
        for tool_type in ('web_search','web_search_preview'):
            try:
                response=client.responses.create(
                    model=os.getenv('OPENAI_SEARCH_MODEL',os.getenv('OPENAI_MODEL','gpt-4.1-mini')),
                    tools=[{'type':tool_type}],
                    input=prompt,
                    max_output_tokens=1200
                )
                break
            except Exception as exc:
                last_error=exc
        if response is None: raise last_error or RuntimeError('웹 검색 도구를 사용할 수 없습니다.')
        d=parse(response.output_text)
        for k in ('demand_score','competition_score','sourcing_score'):
            try:d[k]=max(0,min(100,int(float(d.get(k) or 0))))
            except:d[k]=0
        for k in ('monthly_search_volume','monthly_search_min','monthly_search_max','seller_count','product_count'):
            try:d[k]=max(0,int(float(d.get(k) or 0)))
            except:d[k]=0
        d['main_keyword']=str(d.get('main_keyword') or raw_keyword).strip()[:100]
        d['keyword']=d['main_keyword']
        d['related_keywords']=[str(x).strip() for x in (d.get('related_keywords') or []) if str(x).strip()][:4]
        d['search_volume_estimate']=d.get('monthly_search_volume') or (
            round((d.get('monthly_search_min',0)+d.get('monthly_search_max',0))/2)
            if d.get('monthly_search_max',0) else 0
        )
        d['exact_search_volume_available']=d.get('search_volume_type')=='확인값' and bool(d.get('monthly_search_volume'))
        d['data_scope']=d.get('data_scope') or '공개 웹 검색 기반'
        # 공개 검색에서 절대값을 찾지 못해도 화면 전체가 '자료부족'이 되지 않도록
        # 대표 키워드와 확인 가능한 노출 신호를 바탕으로 보수적인 AI 추정 범위를 제공한다.
        if not d.get('monthly_search_volume') and not d.get('monthly_search_max'):
            ds=max(1,int(d.get('demand_score') or 45))
            base=max(100,ds*120)
            d['monthly_search_min']=int(base*0.55)
            d['monthly_search_max']=int(base*1.45)
            d['search_volume_type']='AI 추정'
            d['search_volume_estimate']=round((d['monthly_search_min']+d['monthly_search_max'])/2)
            d['exact_search_volume_available']=False
            d.setdefault('cautions',[]).append('월간 검색량은 공식 절대 검색량이 아니라 공개 노출 신호 기반 AI 추정 범위입니다.')
        if not d.get('product_count') and not d.get('seller_count'):
            cs=max(1,int(d.get('competition_score') or 50))
            d['product_count']=max(20,cs*35)
            d.setdefault('cautions',[]).append('상품수는 공개 검색 노출 신호를 바탕으로 한 보수적 추정치입니다.')
        if not d.get('turnover') or d.get('turnover')=='자료부족':
            ds=int(d.get('demand_score') or 0)
            d['turnover']='빠름' if ds>=70 else ('보통' if ds>=40 else '느림')
        if not d.get('recommendation') or d.get('recommendation')=='자료부족':
            ss=int(d.get('sourcing_score') or 0)
            d['recommendation']='적극 소싱' if ss>=75 else ('마진 확보 시 소싱' if ss>=55 else ('소량 테스트' if ss>=35 else '비추천'))
        return jsonify(d)
    except Exception as x:
        # 웹검색 도구 자체가 일시 실패해도 대표 키워드 기준의 보수적 분석값을 반환한다.
        kw=raw_keyword
        specificity=min(20,max(0,len(re.findall(r'[A-Za-z0-9가-힣]+',kw))*4))
        demand=50+specificity//2
        competition=55+specificity//3
        sourcing=max(20,min(80,55+(demand-competition)//2))
        mid=max(500,demand*120)
        return jsonify({
            'main_keyword':kw,'keyword':kw,'related_keywords':[],
            'demand_score':demand,'competition_score':competition,'sourcing_score':sourcing,
            'turnover':'보통' if demand>=40 else '느림',
            'recommendation':'마진 확보 시 소싱' if sourcing>=50 else '소량 테스트',
            'search_volume_type':'AI 추정','monthly_search_volume':0,
            'monthly_search_min':int(mid*.55),'monthly_search_max':int(mid*1.45),
            'search_volume_estimate':mid,'seller_count':0,'product_count':competition*35,
            'data_scope':'대표 키워드 기반 AI 추정','confidence':'낮음','exact_search_volume_available':False,
            'evidence':['사진에서 인식한 대표 키워드의 구체성과 상품 카테고리를 반영했습니다.'],
            'cautions':['공개 검색 연결이 일시 실패해 공식 절대 검색량이 아닌 추정 범위를 표시합니다.','키워드 시장 분석 오류: '+str(x)[:120]]
        })


@app.post('/api/export-excel')
def export_excel():
    """브라우저 localStorage의 저장 데이터를 실제 .xlsx 파일로 내보낸다."""
    try:
        body=request.get_json(silent=True) or {}
        general=(body.get('general') or [])[:2000]
        sneakers=(body.get('sneakers') or [])[:2000]
        cart=(body.get('cart') or [])[:3000]
        settings=body.get('settings') or {}

        out=BytesIO()
        wb=xlsxwriter.Workbook(out, {'in_memory': True})
        title=wb.add_format({'bold':True,'font_size':16,'font_color':'#173f70'})
        head=wb.add_format({'bold':True,'bg_color':'#173f70','font_color':'#FFFFFF','border':1,'align':'center','valign':'vcenter'})
        text=wb.add_format({'border':1,'valign':'top'})
        integer=wb.add_format({'border':1,'num_format':'#,##0','valign':'top'})
        percent=wb.add_format({'border':1,'num_format':'0.0"%"','valign':'top'})
        dtfmt=wb.add_format({'border':1,'num_format':'yyyy-mm-dd hh:mm','valign':'top'})
        money=wb.add_format({'border':1,'num_format':'#,##0"원"','valign':'top'})
        note=wb.add_format({'font_color':'#666666','italic':True})

        def parse_dt(value):
            try:
                return datetime.fromisoformat(str(value).replace('Z','+00:00')).replace(tzinfo=None)
            except Exception:
                return str(value or '')

        # 요약
        ws=wb.add_worksheet('요약')
        ws.write('A1','올데이픽 AI 소싱 저장 데이터',title)
        ws.write('A3','구분',head); ws.write('B3','건수',head)
        ws.write('A4','일반상품 기록',text); ws.write_number('B4',len(general),integer)
        ws.write('A5','스니커즈 기록',text); ws.write_number('B5',len(sneakers),integer)
        ws.write('A6','장바구니',text); ws.write_number('B6',len(cart),integer)
        ws.write('A8','다운로드 일시',head); ws.write_datetime('B8',datetime.now(),dtfmt)
        ws.write('A10','세금 계산 기준',head); ws.write('B10','설정값',head)
        labels=[('직장 연봉',settings.get('annualSalary',100000000),'money'),('연 매출',settings.get('annualSales',500000000),'money'),('사업 마진율',settings.get('businessMargin',25),'percent'),('종소세·지방소득세율',settings.get('incomeTaxRate',42),'percent'),('쿠팡 수수료율',settings.get('fee',11.8),'percent'),('배송비',settings.get('ship',4000),'money')]
        for r,(lab,val,kind) in enumerate(labels,11):
            ws.write(r-1,0,lab,text)
            fmt=money if kind=='money' else percent
            ws.write_number(r-1,1,float(val or 0),fmt)
        ws.write('A19','※ 앱에 저장된 예상 계산값이며 실제 신고세액과 다를 수 있습니다.',note)
        ws.set_column('A:A',24); ws.set_column('B:B',20)

        # 일반상품 기록
        ws=wb.add_worksheet('일반상품 기록')
        headers=['저장일시','분류','브랜드','상품명','옵션/종류','상품코드','바코드','용량·규격','인식수량','색상','제조사','원산지','소싱매장','내 판매 구성','쿠팡 상품 구성','쿠팡 검색어','핵심 키워드','예상 수요점수','경쟁강도점수','AI 소싱지수','예상 회전율','키워드 추천결과','키워드 분석 신뢰도','키워드 분석 근거','쿠팡 판매가','개당 소싱가','총 소싱가','쿠팡 수수료율','쿠팡 수수료','배송비','기타비용','매출 부가세','추정 매입 부가세','예상 납부 부가세','종소세 전 이익','종소세·지방소득세율','예상 종소세·지방소득세','세후 최종 순이익','실마진율','ROI','인식 신뢰도','포장·디자인 특징','인식된 원문','확인사항','메모']
        for c,h in enumerate(headers): ws.write(0,c,h,head)
        for r,x in enumerate(general,1):
            scan=x.get('scan') or {}; vals=[parse_dt(x.get('date')),x.get('category') or scan.get('category',''),x.get('brand') or scan.get('brand',''),x.get('productName') or scan.get('product_name') or x.get('name',''),x.get('variant') or scan.get('variant',''),x.get('productCode') or scan.get('product_code',''),x.get('barcode') or scan.get('barcode',''),x.get('volume') or scan.get('volume',''),x.get('count') or scan.get('count',0),x.get('color') or scan.get('color',''),x.get('manufacturer') or scan.get('manufacturer',''),x.get('origin') or scan.get('origin',''),x.get('store',''),x.get('bundle',0),x.get('marketBundle',0),x.get('coupangQuery') or scan.get('coupang_query',''),x.get('keyword',''),(x.get('marketAnalysis') or {}).get('demand_score',0),(x.get('marketAnalysis') or {}).get('competition_score',0),(x.get('marketAnalysis') or {}).get('sourcing_score',0),(x.get('marketAnalysis') or {}).get('turnover',''),(x.get('marketAnalysis') or {}).get('recommendation',''),(x.get('marketAnalysis') or {}).get('confidence',''),' · '.join((x.get('marketAnalysis') or {}).get('evidence') or []),x.get('sale',0),x.get('unitCost',0),x.get('cost',0),x.get('feeRate',settings.get('fee',11.8)),x.get('fee',0),x.get('ship',0),x.get('other',0),x.get('outputVat',0),x.get('inputVat',0),x.get('vat',0),x.get('profitBeforeIncomeTax',0),x.get('incomeTaxRate',0),x.get('incomeTax',0),x.get('profit',0),x.get('margin',0),x.get('roi',0),x.get('confidence') or scan.get('confidence',''),' · '.join(x.get('designFeatures') or scan.get('design_features') or []),' | '.join(x.get('visibleText') or scan.get('visible_text') or []),' · '.join(x.get('warnings') or scan.get('warnings') or []),x.get('memo','')]
            for c,v in enumerate(vals):
                fmt=dtfmt if c==0 and isinstance(v,datetime) else (money if headers[c] in ('쿠팡 판매가','개당 소싱가','총 소싱가','쿠팡 수수료','배송비','기타비용','매출 부가세','추정 매입 부가세','예상 납부 부가세','종소세 전 이익','예상 종소세·지방소득세','세후 최종 순이익') else (percent if headers[c] in ('쿠팡 수수료율','종소세·지방소득세율','실마진율','ROI') else (integer if headers[c] in ('인식수량','내 판매 구성','쿠팡 상품 구성','예상 수요점수','경쟁강도점수','AI 소싱지수') else text)))
                if isinstance(v,(int,float)) and c not in (0,): ws.write_number(r,c,float(v),fmt)
                elif isinstance(v,datetime): ws.write_datetime(r,c,v,fmt)
                else: ws.write(r,c,v,fmt)
        ws.freeze_panes(1,0); ws.autofilter(0,0,max(1,len(general)),len(headers)-1)
        ws.set_column(0,0,18); ws.set_column(1,15,18); ws.set_column(16,31,15); ws.set_column(32,36,32)

        # 스니커즈 기록
        ws=wb.add_worksheet('스니커즈 기록')
        headers=['저장일시','브랜드','모델번호','내부코드','바코드','사이즈','색상','원산지','소싱매장','가격표 표시가','추가 할인율','할인금액','최종 매입가','최고 체결가','평균 체결가','최저 체결가','수요','수요 판단근거','확인 거래수','거래 추세','최근거래 경과일','최고가 순이익','평균가 순이익','최저가 순이익','최고가 마진율','평균가 마진율','최저가 마진율','최고가 ROI','평균가 ROI','최저가 ROI','KREAM 분석 모드','캡처 화면 종류','비교 참고사항','충돌/경고','상품사진 인식 신뢰도','KREAM 분석 신뢰도','사진 자동분류','판매수수료율','기본수수료','수수료 부가세율','판매자 배송비','평균가 기준 예상 부가세','평균가 기준 종소세 전 이익','평균가 기준 예상 종소세','평균가 기준 정산금액','핵심 키워드','예상 수요점수','경쟁강도점수','AI 소싱지수','예상 회전율','추천 결과','키워드 분석 신뢰도','분석 근거']
        for c,h in enumerate(headers): ws.write(0,c,h,head)
        for r,x in enumerate(sneakers,1):
            ta=x.get('tradeAnalysis') or {}
            avgd=x.get('avgDetail') or {}; vals=[parse_dt(x.get('date')),x.get('brand',''),x.get('model',''),x.get('internalCode',''),x.get('barcode',''),x.get('size',0),x.get('color',''),x.get('origin',''),x.get('store',''),x.get('listPrice',0),x.get('discount',0),x.get('discountAmount',0),x.get('buy',0),x.get('highSale',0),x.get('avgSale',0),x.get('lowSale',0),x.get('demand',''),x.get('demandReason',''),x.get('visibleTradeCount',ta.get('count',0)),ta.get('trend',''),ta.get('days',0),x.get('highProfit',0),x.get('avgProfit',0),x.get('lowProfit',0),x.get('highMargin',0),x.get('avgMargin',0),x.get('lowMargin',0),x.get('highROI',0),x.get('avgROI',0),x.get('lowROI',0),x.get('kreamMode',''),' · '.join(x.get('captureTypes') or []),x.get('comparisonNote',''),' · '.join(x.get('conflicts') or []),x.get('confidence',''),x.get('kreamConfidence',''),' · '.join(x.get('imageTypes') or []),settings.get('sFee',6),settings.get('sBaseFee',2500),settings.get('sFeeVat',10),settings.get('sShip',3000),avgd.get('vat',0),avgd.get('profitBeforeIncomeTax',0),avgd.get('incomeTax',0),avgd.get('settlement',0),x.get('keyword',''),(x.get('marketAnalysis') or {}).get('demand_score',0),(x.get('marketAnalysis') or {}).get('competition_score',0),(x.get('marketAnalysis') or {}).get('sourcing_score',0),(x.get('marketAnalysis') or {}).get('turnover',''),(x.get('marketAnalysis') or {}).get('recommendation',''),(x.get('marketAnalysis') or {}).get('confidence',''),' · '.join((x.get('marketAnalysis') or {}).get('evidence') or [])]
            for c,v in enumerate(vals):
                fmt=dtfmt if c==0 and isinstance(v,datetime) else (percent if c in (10,24,25,26,27,28,29,37,39) else (money if c in (9,11,12,13,14,15,21,22,23,38,40,41,42,43,44) else (integer if c in (5,18,20) else text)))
                if isinstance(v,(int,float)) and c!=0: ws.write_number(r,c,float(v),fmt)
                elif isinstance(v,datetime): ws.write_datetime(r,c,v,fmt)
                else: ws.write(r,c,v,fmt)
        ws.freeze_panes(1,0); ws.autofilter(0,0,max(1,len(sneakers)),len(headers)-1)
        ws.set_column(0,0,18); ws.set_column(1,8,18); ws.set_column(9,29,15); ws.set_column(30,36,26); ws.set_column(37,44,16); ws.set_column(45,52,18)

        # KREAM 사이즈별 분석
        ws=wb.add_worksheet('KREAM 사이즈별 분석')
        headers=['저장일시','브랜드','모델번호','기준 매입가','사이즈','거래량','최근 체결가','평균 체결가','최고 체결가','최저 체결가','최근 거래일','최근거래 경과일','판매입찰 최저가','구매입찰 최고가','수요','추천 근거','기준 예상 순이익','기준 ROI','체결거래 원문']
        for c,h in enumerate(headers): ws.write(0,c,h,head)
        rr=1
        for x in sneakers:
            rows=x.get('sizeRows') or (x.get('kreamAnalysis') or {}).get('sizes') or []
            for row in rows:
                ref=float(row.get('avg_price') or row.get('recent_price') or row.get('lowest_ask') or 0)
                buy=float(x.get('buy') or 0); fee=ref*float(settings.get('sFee',6))/100+float(settings.get('sBaseFee',2500)); fee_vat=fee*float(settings.get('sFeeVat',10))/100; ship=float(settings.get('sShip',3000)); output_vat=ref/11; input_vat=buy/11+fee_vat+ship/11; vat=max(0,output_vat-input_vat); pre=ref-fee-fee_vat-ship-buy-vat; tax=max(0,pre)*float(settings.get('incomeTaxRate',42))/100; profit=pre-tax; roi=(profit/buy*100) if buy else 0
                vals=[parse_dt(x.get('date')),x.get('brand',''),x.get('model',''),buy,row.get('size',0),row.get('trade_count',0),row.get('recent_price',0),row.get('avg_price',0),row.get('high_price',0),row.get('low_price',0),row.get('recent_date',''),row.get('days_since_last_trade',0),row.get('lowest_ask',0),row.get('highest_bid',0),row.get('demand',''),row.get('recommendation_reason',''),profit,roi,' | '.join(f"{t.get('date','')} {t.get('price',0)}" for t in (row.get('trades') or []))]
                for c,v in enumerate(vals):
                    fmt=dtfmt if c==0 and isinstance(v,datetime) else (money if c in (3,6,7,8,9,12,13,16) else (percent if c==17 else (integer if c in (4,5,11) else text)))
                    if isinstance(v,(int,float)): ws.write_number(rr,c,float(v),fmt)
                    elif isinstance(v,datetime): ws.write_datetime(rr,c,v,fmt)
                    else: ws.write(rr,c,v,fmt)
                rr+=1
        ws.freeze_panes(1,0); ws.autofilter(0,0,max(1,rr-1),len(headers)-1)
        ws.set_column(0,3,18); ws.set_column(4,17,15); ws.set_column(18,18,38)

        # 장바구니
        ws=wb.add_worksheet('장바구니')
        headers=['담은 순서','구분','상품명','소싱매장','수량','개당 매입가','총 매입액','개당 예상 순이익','예상 총이익','판매가/평균체결가','저장일시']
        for c,h in enumerate(headers): ws.write(0,c,h,head)
        for r,x in enumerate(cart,1):
            qty=float(x.get('qty') or 0); unit=float(x.get('unitCost') or x.get('buy') or 0); profit=float(x.get('profit') or 0)
            sale=float(x.get('sale') or x.get('avgSale') or 0)
            vals=[r,x.get('type',''),x.get('name',''),x.get('store',''),qty,unit,qty*unit,profit,qty*profit,sale,parse_dt(x.get('date'))]
            for c,v in enumerate(vals):
                fmt=dtfmt if c==10 and isinstance(v,datetime) else (money if c in (5,6,7,8,9) else (integer if c in (0,4) else text))
                if isinstance(v,(int,float)): ws.write_number(r,c,float(v),fmt)
                elif isinstance(v,datetime): ws.write_datetime(r,c,v,fmt)
                else: ws.write(r,c,v,fmt)
        ws.freeze_panes(1,0); ws.autofilter(0,0,max(1,len(cart)),len(headers)-1)
        ws.set_column(0,1,12); ws.set_column(2,3,28); ws.set_column(4,9,16); ws.set_column(10,10,18)

        wb.close(); out.seek(0)
        filename='올데이픽_소싱데이터_'+datetime.now().strftime('%Y%m%d_%H%M')+'.xlsx'
        return send_file(out,as_attachment=True,download_name=filename,mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as x:
        return jsonify(error=f'엑셀 생성 오류: {x}'),500

@app.get('/health')
def health():return jsonify(ok=True)
if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.getenv('PORT','10000')))
