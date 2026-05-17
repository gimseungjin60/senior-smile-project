import {onCall, HttpsError} from "firebase-functions/v2/https";
import {getFirestore} from "firebase-admin/firestore";
import {getAuth} from "firebase-admin/auth";
import {defineString} from "firebase-functions/params";

const LIVEKIT_API_KEY = defineString("LIVEKIT_API_KEY");
const LIVEKIT_API_SECRET = defineString("LIVEKIT_API_SECRET");
const LIVEKIT_URL = defineString("LIVEKIT_URL");

export const getLiveKitToken = onCall(
  {region: "asia-northeast3", invoker: "public"},
  async (request) => {
    let uid = request.auth?.uid;
    if (!uid) {
      const {idToken} = request.data as { idToken?: string };
      if (!idToken) throw new HttpsError("unauthenticated", "로그인이 필요합니다");
      try {
        const decoded = await getAuth().verifyIdToken(idToken);
        uid = decoded.uid;
      } catch {
        throw new HttpsError("unauthenticated", "유효하지 않은 인증 토큰입니다");
      }
    }

    const {deviceId} = request.data as { deviceId: string };
    if (!deviceId) throw new HttpsError("invalid-argument", "deviceId가 필요합니다");

    // 페어링된 사용자만 토큰 발급 — pairedDeviceIds(string[]) 기반으로 검증
    const db = getFirestore();
    const userSnap = await db.collection("users").doc(uid).get();
    const pairedDeviceIds: string[] = (userSnap.data()?.pairedDeviceIds as string[] | undefined) ?? [];

    if (!pairedDeviceIds.includes(deviceId)) {
      throw new HttpsError("permission-denied", "해당 기기에 페어링되지 않았습니다");
    }

    // LiveKit Access Token 동적 import (ESM 모듈)
    const {AccessToken} = await import("livekit-server-sdk");
    const at = new AccessToken(LIVEKIT_API_KEY.value(), LIVEKIT_API_SECRET.value(), {
      identity: uid,
      ttl: "5m",
    });
    at.addGrant({roomJoin: true, room: `device-${deviceId}`, canSubscribe: true});

    return {
      token: await at.toJwt(),
      url: LIVEKIT_URL.value(),
    };
  }
);
