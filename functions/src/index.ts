import {initializeApp} from "firebase-admin/app";

initializeApp();

export {verifyPairing} from "./verifyPairing";
export {unpairDevice} from "./unpairDevice";
export {getLiveKitToken} from "./getLiveKitToken";
export {cleanupExpiredPins} from "./cleanupExpiredPins";
