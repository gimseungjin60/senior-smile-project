import {onSchedule} from "firebase-functions/v2/scheduler";
import {getFirestore, Timestamp} from "firebase-admin/firestore";

export const cleanupExpiredPins = onSchedule(
  {schedule: "every 10 minutes", region: "asia-northeast3"},
  async () => {
    const db = getFirestore();
    const now = Timestamp.now();

    const snap = await db
      .collection("pairing_requests")
      .where("expiresAt", "<", now)
      .get();

    if (snap.empty) return;

    const batch = db.batch();
    snap.docs.forEach((doc) => batch.delete(doc.ref));
    await batch.commit();
  }
);
