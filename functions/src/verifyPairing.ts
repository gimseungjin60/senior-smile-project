import {onCall, HttpsError} from "firebase-functions/v2/https";
import {getFirestore, FieldValue, Timestamp} from "firebase-admin/firestore";
import {getAuth} from "firebase-admin/auth";

export const verifyPairing = onCall(
  {region: "asia-northeast3", invoker: "public"},
  async (request) => {
    // React Native에서 callable SDK가 auth 헤더를 누락하는 케이스 대비:
    // request.data.idToken으로 직접 검증
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

    const {code} = request.data as { code: string };
    if (!code || typeof code !== "string" || code.length !== 6) {
      throw new HttpsError("invalid-argument", "6자리 PIN을 입력해주세요");
    }

    const db = getFirestore();

    return await db.runTransaction(async (tx) => {
      const pinRef = db.collection("pairing_requests").doc(code);
      const pinSnap = await tx.get(pinRef);

      if (!pinSnap.exists) {
        throw new HttpsError("not-found", "유효하지 않은 PIN입니다");
      }

      const pinData = pinSnap.data()!;

      if (pinData.claimed) {
        throw new HttpsError("already-exists", "이미 사용된 PIN입니다");
      }

      const expiresAt = pinData.expiresAt as Timestamp;
      if (expiresAt.toMillis() < Date.now()) {
        throw new HttpsError("deadline-exceeded", "만료된 PIN입니다. 새 PIN을 요청해주세요");
      }

      const deviceId: string = pinData.deviceId;

      // 원자적 트랜잭션: PIN 소비 + pairing 기록 동시 처리
      tx.update(pinRef, {claimed: true, claimedBy: uid, claimedAt: Timestamp.now()});

      tx.set(
        db.collection("users").doc(uid),
        {
          pairings: FieldValue.arrayUnion({
            deviceId,
            pairedAt: Timestamp.now(),
          }),
          pairedDeviceIds: FieldValue.arrayUnion(deviceId),
        },
        {merge: true}
      );

      tx.set(
        db.collection("devices").doc(deviceId),
        {
          paired: true,
          pairedBy: uid,
          pairedAt: Timestamp.now(),
          pairedUids: FieldValue.arrayUnion(uid),
        },
        {merge: true}
      );

      return {success: true, deviceId};
    });
  }
);
