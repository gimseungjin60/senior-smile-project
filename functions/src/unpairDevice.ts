import {onCall, HttpsError} from "firebase-functions/v2/https";
import {getFirestore, FieldValue} from "firebase-admin/firestore";
import {getAuth} from "firebase-admin/auth";

export const unpairDevice = onCall(
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
    if (!deviceId || typeof deviceId !== "string") {
      throw new HttpsError("invalid-argument", "deviceId가 필요합니다");
    }

    const db = getFirestore();

    return await db.runTransaction(async (tx) => {
      const deviceRef = db.collection("devices").doc(deviceId);
      const userRef = db.collection("users").doc(uid!);

      const deviceSnap = await tx.get(deviceRef);
      const currentUids: string[] = deviceSnap.exists ?
        ((deviceSnap.data()?.pairedUids as string[] | undefined) ?? []) :
        [];
      const remainingUids = currentUids.filter((x) => x !== uid);

      // users.pairings (객체 배열) 도 함께 정리 — 보호자 앱이 이걸로 isPaired 판단
      const userSnap = await tx.get(userRef);
      const oldPairings = (userSnap.data()?.pairings as Array<{deviceId: string}> | undefined) ?? [];
      const newPairings = oldPairings.filter((p) => p.deviceId !== deviceId);

      tx.set(
        userRef,
        {
          pairings: newPairings,
          pairedDeviceIds: FieldValue.arrayRemove(deviceId),
        },
        {merge: true}
      );

      const deviceUpdate: Record<string, unknown> = {
        pairedUids: FieldValue.arrayRemove(uid!),
      };
      if (remainingUids.length === 0) {
        deviceUpdate.paired = false;
        deviceUpdate.pairedBy = null;
      }
      tx.set(deviceRef, deviceUpdate, {merge: true});

      return {success: true, remaining: remainingUids.length};
    });
  }
);
