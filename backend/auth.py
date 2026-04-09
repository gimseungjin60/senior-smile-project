"""
보호자 인증 시스템
- 회원가입/로그인 (이메일+비밀번호)
- JWT 토큰 발급/검증
- Firestore 또는 로컬 JSON 파일에 사용자 정보 저장
"""

import time
import json
import logging
import hashlib
from pathlib import Path
from typing import Optional

import jwt
import bcrypt

import config

logger = logging.getLogger(__name__)

# JWT 설정
_raw_secret = (config.OPENAI_API_KEY or "default-dev-key") + "senior-smile-jwt-secret-2026"
JWT_SECRET = hashlib.sha256(_raw_secret.encode()).hexdigest()
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 72

# 로컬 사용자 저장소 (Firebase 없을 때 폴백)
_LOCAL_USERS_FILE = Path(__file__).parent / "users_local.json"


def _load_local_users() -> dict:
    if _LOCAL_USERS_FILE.exists():
        return json.loads(_LOCAL_USERS_FILE.read_text(encoding="utf-8"))
    return {}


def _save_local_users(users: dict):
    _LOCAL_USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def _create_token(user_id: str, name: str) -> str:
    payload = {
        "user_id": user_id,
        "name": name,
        "exp": int(time.time()) + JWT_EXPIRE_HOURS * 3600,
        "iat": int(time.time()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> Optional[dict]:
    """JWT 토큰을 검증하고 payload를 반환합니다."""
    if not token:
        return None
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def signup(db, email: str, password: str, name: str) -> dict:
    """회원가입"""
    if not email or not password or not name:
        return {"success": False, "error": "이메일, 비밀번호, 이름을 모두 입력해주세요."}

    if len(password) < 4:
        return {"success": False, "error": "비밀번호는 4자 이상이어야 합니다."}

    user_id = f"user_{hashlib.md5(email.encode()).hexdigest()[:10]}"
    hashed_pw = _hash_password(password)

    user_data = {
        "user_id": user_id,
        "email": email,
        "name": name,
        "password": hashed_pw,
        "role": "보호자",
        "created_at": time.time(),
        "device_id": None,
        "family_id": None,
    }

    if db:
        # Firestore 모드
        try:
            existing = list(db.collection("users").where("email", "==", email).limit(1).stream())
            if existing:
                return {"success": False, "error": "이미 가입된 이메일입니다."}
            db.collection("users").document(user_id).set(user_data)
        except Exception as e:
            logger.error(f"[Auth] Firestore 저장 실패: {e}")
            return {"success": False, "error": "서버 오류가 발생했습니다."}
    else:
        # 로컬 파일 모드
        users = _load_local_users()
        for u in users.values():
            if u.get("email") == email:
                return {"success": False, "error": "이미 가입된 이메일입니다."}
        users[user_id] = user_data
        _save_local_users(users)

    token = _create_token(user_id, name)
    logger.info(f"[Auth] 회원가입 완료: {email}")

    return {
        "success": True,
        "token": token,
        "user": {"user_id": user_id, "email": email, "name": name},
    }


def login(db, email: str, password: str) -> dict:
    """로그인"""
    if not email or not password:
        return {"success": False, "error": "이메일과 비밀번호를 입력해주세요."}

    user_data = None

    if db:
        # Firestore 모드
        try:
            docs = list(db.collection("users").where("email", "==", email).limit(1).stream())
            if docs:
                user_data = docs[0].to_dict()
        except Exception as e:
            logger.error(f"[Auth] Firestore 조회 에러: {e}")
            return {"success": False, "error": "서버 오류가 발생했습니다."}
    else:
        # 로컬 파일 모드
        users = _load_local_users()
        for u in users.values():
            if u.get("email") == email:
                user_data = u
                break

    if not user_data:
        return {"success": False, "error": "이메일 또는 비밀번호가 올바르지 않습니다."}

    if not _verify_password(password, user_data.get("password", "")):
        return {"success": False, "error": "이메일 또는 비밀번호가 올바르지 않습니다."}

    token = _create_token(user_data["user_id"], user_data["name"])
    logger.info(f"[Auth] 로그인 성공: {email}")

    return {
        "success": True,
        "token": token,
        "user": {
            "user_id": user_data["user_id"],
            "email": user_data["email"],
            "name": user_data["name"],
            "device_id": user_data.get("device_id"),
            "family_id": user_data.get("family_id"),
        },
    }


def get_user(db, user_id: str) -> Optional[dict]:
    """사용자 정보를 조회합니다."""
    if db:
        try:
            doc = db.collection("users").document(user_id).get()
            if doc.exists:
                user = doc.to_dict()
                user.pop("password", None)
                return user
        except Exception:
            pass
    else:
        users = _load_local_users()
        if user_id in users:
            user = dict(users[user_id])
            user.pop("password", None)
            return user
    return None
