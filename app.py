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
@app.get('/health')
def health():return jsonify(ok=True)
if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.getenv('PORT','10000')))
