"""
OpenAI Realtime API 격리 PoC (+실제 데이터 도구) — 기존 백엔드/엔드포인트 전혀 안 건드림.
단독 FastAPI 서버(:8003). 브라우저 마이크 ↔ Realtime WebRTC + function calling(날씨/시간).

실행: cd backend && python scripts/realtime_poc.py
테스트: 노트북 브라우저 http://localhost:8003 → "시작" → "오늘 날씨 어때?" 등 말 걸기.
(localhost = secure context, 마이크 자동 허용 / chrome flags 불필요)
"""
import pathlib
import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

ENV = pathlib.Path(__file__).resolve().parents[1] / ".env"
MODEL = "gpt-realtime"


def _env(key: str) -> str:
    try:
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


OPENAI_KEY = _env("OPENAI_API_KEY")
WEATHER_KEY = _env("WEATHER_API_KEY")
WEATHER_CITY = _env("WEATHER_CITY") or "Seoul"

TOOLS = [
    {"type": "function", "name": "get_weather",
     "description": "현재 실제 날씨(기온/상태)를 가져온다. 날씨를 물으면 반드시 호출.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "function", "name": "get_time",
     "description": "지금 현재 시각과 날짜를 가져온다. 시간/날짜를 물으면 반드시 호출.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
]

INSTRUCTIONS = (
    "너는 '앨범이'라는 이름의 일곱 살 손주야. 한국 할머니·할아버지와 다정하게 한국어로만 "
    "이야기해. 항상 한두 문장으로 짧고 따뜻하게, '헤헤~' 같은 애교를 살짝 섞어서. "
    "날씨·시간처럼 실제 정보는 반드시 도구(get_weather/get_time)를 호출해서 진짜 값으로 알려줘. "
    "모르는 건 솔직히 모른다고 하고 대화를 자연스럽게 이어가."
)

app = FastAPI()


@app.get("/token")
async def token():
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            "https://api.openai.com/v1/realtime/client_secrets",
            headers={"Authorization": f"Bearer {OPENAI_KEY}",
                     "Content-Type": "application/json"},
            json={"session": {
                "type": "realtime",
                "model": MODEL,
                "instructions": INSTRUCTIONS,
                "tools": TOOLS,
                "tool_choice": "auto",
            }},
        )
    return JSONResponse(status_code=r.status_code, content=r.json())


@app.get("/weather")
async def weather():
    """실제 날씨 (OpenWeather). function_call_output 으로 모델에 전달됨."""
    if not WEATHER_KEY:
        return {"error": "WEATHER_API_KEY 미설정"}
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get("https://api.openweathermap.org/data/2.5/weather",
                        params={"q": WEATHER_CITY, "appid": WEATHER_KEY,
                                "units": "metric", "lang": "kr"})
    if r.status_code != 200:
        return {"error": f"weather api {r.status_code}", "body": r.text[:200]}
    d = r.json()
    return {
        "city": WEATHER_CITY,
        "temp_c": round(d["main"]["temp"]),
        "feels_like_c": round(d["main"]["feels_like"]),
        "description": d["weather"][0]["description"],
    }


HTML = """<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Realtime PoC</title></head>
<body style="font-family:sans-serif;padding:24px;max-width:680px;margin:auto">
<h2>앨범이 — Realtime 음성 대화 (실데이터)</h2>
<button id=start style="font-size:22px;padding:14px 22px;border-radius:10px">🎤 시작 (누르고 말 걸기)</button>
<button id=stop style="font-size:16px;padding:10px;margin-left:8px" disabled>■ 정지</button>
<p style="color:#666">예: "오늘 날씨 어때?", "지금 몇 시야?", "심심해"</p>
<pre id=log style="white-space:pre-wrap;background:#f4f4f4;padding:12px;border-radius:8px;min-height:140px"></pre>
<audio id=aud autoplay></audio>
<script>
const logEl=document.getElementById('log')
const log=(m)=>{logEl.textContent+=m+"\\n"; logEl.scrollTop=logEl.scrollHeight}
let pc, ms, dc
async function handleTool(name, call_id){
  let out
  try{
    if(name==='get_weather') out=await fetch('/weather').then(r=>r.json())
    else if(name==='get_time') out={now:new Date().toLocaleString('ko-KR')}
    else out={error:'unknown tool'}
  }catch(e){ out={error:String(e)} }
  log('🔧 '+name+' → '+JSON.stringify(out))
  dc.send(JSON.stringify({type:'conversation.item.create',
    item:{type:'function_call_output', call_id, output:JSON.stringify(out)}}))
  dc.send(JSON.stringify({type:'response.create'}))
}
document.getElementById('start').onclick=async()=>{
  document.getElementById('start').disabled=true
  try{
    log('① 토큰 요청...')
    const t=await fetch('/token').then(r=>r.json())
    const ek=t.value || (t.client_secret&&t.client_secret.value)
    if(!ek){log('✗ 토큰 실패: '+JSON.stringify(t)); return}
    log('② 토큰 OK')
    pc=new RTCPeerConnection()
    pc.ontrack=(e)=>{document.getElementById('aud').srcObject=e.streams[0]; log('④ 오디오 수신 → 재생')}
    ms=await navigator.mediaDevices.getUserMedia({audio:true})
    pc.addTrack(ms.getTracks()[0])
    dc=pc.createDataChannel('oai-events')
    dc.onmessage=(e)=>{ try{const o=JSON.parse(e.data)
      if(o.type==='response.function_call_arguments.done') handleTool(o.name,o.call_id)
      else if(o.type==='error') log('   ⚠ '+JSON.stringify(o.error||o).slice(0,160))
    }catch{} }
    log('③ 마이크 OK, 연결 중...')
    const offer=await pc.createOffer(); await pc.setLocalDescription(offer)
    const resp=await fetch('https://api.openai.com/v1/realtime/calls?model=gpt-realtime',{
      method:'POST', body:offer.sdp,
      headers:{Authorization:'Bearer '+ek,'Content-Type':'application/sdp'}})
    if(!resp.ok){log('✗ SDP 실패 HTTP '+resp.status+'\\n'+(await resp.text()).slice(0,300)); return}
    await pc.setRemoteDescription({type:'answer', sdp:await resp.text()})
    log('⑤ 연결 완료! 말해보세요. (날씨/시간은 실제 값으로 답합니다)')
    document.getElementById('stop').disabled=false
  }catch(err){log('✗ 에러: '+err.message)}
}
document.getElementById('stop').onclick=()=>{
  try{ms&&ms.getTracks().forEach(t=>t.stop())}catch{}
  try{pc&&pc.close()}catch{}
  log('정지'); document.getElementById('start').disabled=false; document.getElementById('stop').disabled=true
}
</script></body></html>"""


@app.get("/")
async def index():
    return HTMLResponse(HTML)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8003)
