"""
Railway binary-WebSocket 전제 검증용 최소 프로브.
(docs/voice-cloud-refactor-design.md §6 — 가장 위험한 미검증 가정 제거용)

검증 목표: Railway 프록시가 WebSocket **binary 프레임**을 끊거나 버퍼링/병합하지 않고
프레임 경계·바이트를 그대로 왕복시키는가. (음성 재설계의 1번 가정: 브라우저 → binary 업로드 → 서버)

로컬 실행:
    cd backend && uvicorn scripts.ws_binary_probe:app --port 8099
    → 브라우저로 http://localhost:8099/ 열면 자동 테스트 PASS/FAIL 표시

Railway 배포(턴키):
    1) ! railway login          (브라우저 인증 — 세션에서 직접 실행)
    2) railway init             (새 프로젝트 또는 기존 연결)
    3) Start Command 를 아래로 지정 후 배포:
       uvicorn scripts.ws_binary_probe:app --host 0.0.0.0 --port $PORT
    4) 배포된 https://<app>.up.railway.app/ 접속 → PASS 뜨면 binary WS 가정 검증 완료
       (단일 인스턴스: Settings에서 min=max=1 인지도 확인)

이 파일은 본 앱(main.py)과 독립이며 mediapipe/opencv 등 무거운 의존성을 import 하지 않음.
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

app = FastAPI()


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.websocket("/ws/probe")
async def ws_probe(ws: WebSocket):
    """binary 는 바이트 그대로 1:1 에코, text 는 'echo:' 접두해 에코. 프레임 경계 보존 확인용."""
    await ws.accept()
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is not None:
                await ws.send_bytes(data)          # 받은 바이트 그대로 반환
                continue
            text = msg.get("text")
            if text is not None:
                await ws.send_text(f"echo:{text}")
    except WebSocketDisconnect:
        pass


@app.get("/", response_class=HTMLResponse)
async def index():
    # 같은 호스트로 ws/wss 자동 선택. 64KB binary + text 왕복 후 일치 검증.
    return """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>binary WS probe</title>
<style>body{font:16px system-ui;margin:40px;max-width:680px}
#r{padding:16px;border-radius:8px;font-weight:700;font-size:20px}
.pass{background:#dcfce7;color:#166534}.fail{background:#fee2e2;color:#991b1b}
.run{background:#fef9c3;color:#854d0e}pre{background:#f1f5f9;padding:12px;border-radius:8px;white-space:pre-wrap}</style>
</head><body>
<h2>Railway binary WebSocket 프로브</h2>
<div id="r" class="run">테스트 중...</div>
<pre id="log"></pre>
<script>
const r=document.getElementById('r'),log=document.getElementById('log');
const out=(m)=>{log.textContent+=m+"\\n";};
const SIZE=65536;
const scheme=location.protocol==='https:'?'wss':'ws';
const url=`${scheme}://${location.host}/ws/probe`;
out('connect → '+url);
const payload=new Uint8Array(SIZE);
for(let i=0;i<SIZE;i++)payload[i]=(i*7+13)&255;
let gotBytes=false,gotText=false;
function finish(){
  if(gotBytes&&gotText){r.className='pass';r.textContent='PASS — binary 프레임 왕복 OK ('+SIZE+'B 일치)';}
}
const ws=new WebSocket(url);ws.binaryType='arraybuffer';
const t0=performance.now();
ws.onopen=()=>{out('open, send '+SIZE+'B binary');ws.send(payload);};
ws.onmessage=(e)=>{
  if(e.data instanceof ArrayBuffer){
    const got=new Uint8Array(e.data);
    let ok=got.length===SIZE;
    if(ok)for(let i=0;i<SIZE;i++){if(got[i]!==payload[i]){ok=false;break;}}
    out('recv binary '+got.length+'B '+(ok?'identical':'MISMATCH')+' ('+Math.round(performance.now()-t0)+'ms)');
    if(ok){gotBytes=true;ws.send('hello-probe');}
    else{r.className='fail';r.textContent='FAIL — binary 변형/절단 ('+got.length+'B)';}
  }else{
    out('recv text: '+e.data);
    if(e.data==='echo:hello-probe')gotText=true;
    finish();
  }
};
ws.onerror=()=>{r.className='fail';r.textContent='FAIL — WebSocket 에러(프록시 차단 가능)';};
ws.onclose=(e)=>{out('close code='+e.code);if(!(gotBytes&&gotText)&&r.className!=='fail'){r.className='fail';r.textContent='FAIL — 완료 전 종료(code '+e.code+')';}};
</script></body></html>"""
