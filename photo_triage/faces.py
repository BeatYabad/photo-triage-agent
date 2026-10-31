"""Local, unnamed face comparison. Embeddings are not deletion evidence."""
from pathlib import Path
import hashlib
import numpy as np

MODELS = {
    'face_detection_yunet_2026may.onnx': ('face_detection_yunet', 'ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0'),
    'face_recognition_sface_2021dec.onnx': ('face_recognition_sface', '0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79'),
}
REVISION = '48b10fa505aeb51fbd127fc60a5c660040e823b7'
THRESHOLD = .363  # OpenCV Zoo SFace cosine operating point; protective use only.


def vector(value):
    v = np.asarray(value, dtype=np.float32).reshape(-1)
    if v.size != 128 or not np.isfinite(v).all() or np.linalg.norm(v) < 1e-8:
        raise ValueError('Invalid family face embedding')
    return v / np.linalg.norm(v)


def matches(queries, references):
    if not queries or not references:
        return {'matched': False, 'score': None}
    q = np.stack([vector(v) for v in queries])
    r = np.stack([vector(v) for v in references])
    score = float(np.max(q @ r.T))
    return {'matched': score >= THRESHOLD, 'score': round(score, 5), 'threshold': THRESHOLD}


class FaceEncoder:
    def __init__(self, folder):
        import cv2
        folder = Path(folder)
        for name, (_, expected) in MODELS.items():
            if hashlib.sha256((folder/name).read_bytes()).hexdigest() != expected:
                raise ValueError('Family model hash mismatch: ' + name)
        self.detector = cv2.FaceDetectorYN.create(str(folder/'face_detection_yunet_2026may.onnx'), '',
            (320, 320), .8, .3, 5000, cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_CPU)
        self.recognizer = cv2.FaceRecognizerSF.create(str(folder/'face_recognition_sface_2021dec.onnx'), '',
            cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_CPU)

    def encode(self, image):
        import cv2
        image = image.convert('RGB').copy()
        image.thumbnail((1600, 1600))
        bgr = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        self.detector.setInputSize((bgr.shape[1], bgr.shape[0]))
        _, faces = self.detector.detect(bgr)
        result = []
        for face in ([] if faces is None else faces):
            if min(face[2], face[3]) < 24:
                continue
            crop = self.recognizer.alignCrop(bgr, face)
            result.append(vector(self.recognizer.feature(crop)).tolist())
        return result
