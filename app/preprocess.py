"""Image preparation for OCR. Phone photos of receipts are the hard case: wood-grain backgrounds,
shadows, creases. We crop to the paper, upscale, and produce a few variants; the extractor keeps
whichever variant yields the most self-consistent parse."""
import io

import cv2
import numpy as np
from PIL import Image


def load_image(data: bytes) -> np.ndarray:
    img = Image.open(io.BytesIO(data))
    img.load()
    img = img.convert("RGB")
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def _crop_receipt(img: np.ndarray) -> np.ndarray | None:
    """Find the paper: a large bright, low-saturation region. Returns None if it looks unreliable."""
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = ((hsv[..., 1] < 60) & (hsv[..., 2] > 150)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((15, 15), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    x, y, cw, ch = cv2.boundingRect(max(cnts, key=cv2.contourArea))
    if cw * ch < 0.08 * w * h or cw * ch > 0.9 * w * h:  # tiny blob, or "crop" is the whole frame
        return None
    pad = 10
    return img[max(y - pad, 0) : y + ch + pad, max(x - pad, 0) : x + cw + pad]


def _upscale(gray: np.ndarray, target_w: int = 2000) -> np.ndarray:
    scale = target_w / gray.shape[1]
    if scale > 1.0:
        return cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return gray


def _threshold(gray: np.ndarray) -> np.ndarray:
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 41, 15)


def variants(img: np.ndarray) -> list[tuple[str, np.ndarray]]:
    crop = _crop_receipt(img)
    base = crop if crop is not None else img
    gray = _upscale(cv2.cvtColor(base, cv2.COLOR_BGR2GRAY))
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
    out = [("threshold", _threshold(gray)), ("clahe", clahe)]
    if crop is None:  # no clean paper region (stained / cluttered photo): add a 2x-upscaled pass
        big = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        out.append(("threshold-2x", _threshold(big)))
    return out
