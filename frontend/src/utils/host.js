/**
 * 시니어 디바이스 프론트엔드의 백엔드 호스트 결정 헬퍼.
 *
 * 갤탭(시니어 디스플레이)이 라즈베리파이 IP로 접속하든, 라즈베리 자체에서 띄우든
 * window.location.hostname 으로 자동 라우팅하여 'localhost' 하드코딩을 제거.
 *
 * Vite dev (포트 5173)에서 브라우저로 접속해도 backend는 포트 8000이므로
 * 호스트만 동적으로 가져오고 포트는 명시한다.
 */

const SENIOR_BACKEND_PORT = 8000;     // FastAPI 시니어 백엔드 (자기 자신)
const AIBUM_BACKEND_PORT = 8001;      // 보호자 백엔드 (시연에서 미실행 가능 — fetch 실패는 try/catch)

function _hostname() {
  if (typeof window === 'undefined') return 'localhost';
  return window.location.hostname || 'localhost';
}

/** http://<hostname>:8000 — 시니어 백엔드 REST */
export function seniorHttpUrl() {
  return `http://${_hostname()}:${SENIOR_BACKEND_PORT}`;
}

/** ws://<hostname>:8000 — 시니어 백엔드 WebSocket 베이스 */
export function seniorWsUrl(path = '') {
  return `ws://${_hostname()}:${SENIOR_BACKEND_PORT}${path}`;
}

/** http://<hostname>:8001 — 보호자 백엔드 (시연에서 미실행이면 fetch 실패해도 OK) */
export function aibumHttpUrl() {
  return `http://${_hostname()}:${AIBUM_BACKEND_PORT}`;
}
