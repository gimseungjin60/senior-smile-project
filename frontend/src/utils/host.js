/**
 * 시니어 디바이스 프론트엔드의 백엔드 호스트 결정 헬퍼.
 *
 * 환경 분기:
 * - 로컬 개발(npm run dev, import.meta.env.PROD === false):
 *     window.location.hostname 으로 자동 라우팅 → http://<PC_IP|localhost>:8000.
 *     갤탭이 PC IP로 접속하든 PC 자체 브라우저든 동일하게 동작(로컬 음성 e2e).
 * - 프로덕션 빌드(npm run build, import.meta.env.PROD === true):
 *     클라우드 백엔드(Render) 고정 URL 사용. https 이므로 WS 는 wss 로 자동 승격.
 *
 * 클라우드 URL 은 .env 의 VITE_SENIOR_BACKEND_URL 로 덮어쓸 수 있다(미설정 시 기본값).
 */

const SENIOR_BACKEND_PORT = 8000;     // FastAPI 시니어 백엔드 (로컬 dev)
const AIBUM_BACKEND_PORT = 8001;      // 보호자 백엔드 (시연에서 미실행 가능 — fetch 실패는 try/catch)

// 프로덕션 빌드에서 붙을 클라우드 백엔드(Render). .env 로 덮어쓰기 가능.
const PROD_SENIOR_BACKEND = (
  import.meta.env.VITE_SENIOR_BACKEND_URL || 'https://senior-smile-project.onrender.com'
).replace(/\/+$/, '');   // 끝 슬래시 제거(경로 결합 시 // 방지)

function _hostname() {
  if (typeof window === 'undefined') return 'localhost';
  return window.location.hostname || 'localhost';
}

/** 시니어 백엔드 HTTP 베이스. 로컬=http://<hostname>:8000, 프로덕션=클라우드 https URL */
function _seniorHttpBase() {
  if (import.meta.env.PROD) return PROD_SENIOR_BACKEND;
  return `http://${_hostname()}:${SENIOR_BACKEND_PORT}`;
}

/** 시니어 백엔드 REST 베이스 URL */
export function seniorHttpUrl() {
  return _seniorHttpBase();
}

/**
 * 시니어 백엔드 WebSocket URL.
 * HTTP 베이스에서 프로토콜만 ws 계열로 치환 → http→ws, https→wss.
 * (onrender 는 https 라 wss 필수. 안 맞으면 mixed-content 로 /ws, /ws/voice 가 차단됨)
 */
export function seniorWsUrl(path = '') {
  const wsBase = _seniorHttpBase().replace(/^http/, 'ws');
  return `${wsBase}${path}`;
}

/** http://<hostname>:8001 — 보호자 백엔드 (시연에서 미실행이면 fetch 실패해도 OK) */
export function aibumHttpUrl() {
  return `http://${_hostname()}:${AIBUM_BACKEND_PORT}`;
}
