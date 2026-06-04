/**
 * 시니어 디바이스 프론트엔드의 백엔드 호스트 결정 헬퍼.
 *
 * 로컬(localhost/127.0.0.1): ws:// / http:// + 포트 8000
 * 클라우드(Render 등):       wss:// / https:// + 포트 없음 (443 기본)
 */

const SENIOR_BACKEND_PORT = 8000;

function _hostname() {
  if (typeof window === 'undefined') return 'localhost';
  return window.location.hostname || 'localhost';
}

function _isLocal() {
  const h = _hostname();
  return h === 'localhost' || h === '127.0.0.1';
}

/** http(s)://<host>[:8000] — 시니어 백엔드 REST */
export function seniorHttpUrl() {
  const protocol = _isLocal() ? 'http' : 'https';
  const port = _isLocal() ? `:${SENIOR_BACKEND_PORT}` : '';
  return `${protocol}://${_hostname()}${port}`;
}

/** ws(s)://<host>[:8000] — 시니어 백엔드 WebSocket 베이스 */
export function seniorWsUrl(path = '') {
  const protocol = _isLocal() ? 'ws' : 'wss';
  const port = _isLocal() ? `:${SENIOR_BACKEND_PORT}` : '';
  return `${protocol}://${_hostname()}${port}${path}`;
}

/** http(s)://<host>[:8001] — 보호자 백엔드 (미실행이면 fetch 실패해도 OK) */
export function aibumHttpUrl() {
  const protocol = _isLocal() ? 'http' : 'https';
  const port = _isLocal() ? ':8001' : '';
  return `${protocol}://${_hostname()}${port}`;
}
