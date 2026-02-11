"""Container text recognition using PaddleOCR.

Extracts:
  - ISO container number  (e.g. MSCU 123456-7)
  - Gross weight
  - Tare weight
  - Net / Payload weight
  - Cubic capacity
  - Additional markings (4-6 lines)
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

from monitoring.metrics import (
    ocr_processing_duration,
    ocr_recognitions_success,
    ocr_recognitions_total,
)

logger = logging.getLogger(__name__)

# ── ISO 6346 container code pattern ─────────────────────────
# Owner code (4 alpha) + serial (6 digits) + check digit (1 digit)
ISO_PATTERN = re.compile(r"[A-Z]{4}\s?\d{6,7}[-\s]?\d?")
WEIGHT_PATTERN = re.compile(
    r"(MAX\.?\s*GROSS|GROSS|TARE|NET|PAYLOAD|MAX\.?\s*PAYLOAD)"
    r"\s*[:.]?\s*"
    r"(\d[\d\s,.]*)\s*(KG|LBS?|KGS?)",
    re.IGNORECASE,
)
CAPACITY_PATTERN = re.compile(
    r"(CU\.?\s*CAP|CAPACITY|CU\.?\s*FT|CBM)"
    r"\s*[:.]?\s*"
    r"(\d[\d\s,.]*)\s*(CU\.?\s*FT|CBM|M3)?",
    re.IGNORECASE,
)


@dataclass
class ContainerInfo:
    """Parsed container recognition result."""
    iso_number: Optional[str] = None
    owner_code: Optional[str] = None
    serial_number: Optional[str] = None
    check_digit: Optional[str] = None
    max_gross_weight: Optional[str] = None
    tare_weight: Optional[str] = None
    net_weight: Optional[str] = None
    payload: Optional[str] = None
    cube_capacity: Optional[str] = None
    raw_lines: list[str] = field(default_factory=list)
    confidence: float = 0.0

    def is_valid(self) -> bool:
        return self.iso_number is not None

    def to_dict(self) -> dict:
        return {
            "iso_number": self.iso_number,
            "owner_code": self.owner_code,
            "serial_number": self.serial_number,
            "check_digit": self.check_digit,
            "max_gross_weight": self.max_gross_weight,
            "tare_weight": self.tare_weight,
            "net_weight": self.net_weight,
            "payload": self.payload,
            "cube_capacity": self.cube_capacity,
            "raw_lines": self.raw_lines,
            "confidence": round(self.confidence, 3),
        }


class ContainerOCR:
    """PaddleOCR-based container text recognition engine."""

    def __init__(self, cfg: dict):
        self.confidence_threshold = cfg.get("confidence_threshold", 0.7)
        self.roi_padding = cfg.get("roi_padding", {
            "top": 0.05, "bottom": 0.05, "left": 0.05, "right": 0.05,
        })
        self._init_paddle(cfg)

    def _init_paddle(self, cfg: dict):
        from paddleocr import PaddleOCR

        self.ocr = PaddleOCR(
            lang=cfg.get("lang", "en"),
            use_gpu=cfg.get("use_gpu", True),
            det_db_thresh=cfg.get("det_db_thresh", 0.3),
            rec_batch_num=cfg.get("rec_batch_num", 6),
            show_log=False,
        )
        logger.info("PaddleOCR engine initialized (lang=%s, gpu=%s)",
                     cfg.get("lang", "en"), cfg.get("use_gpu", True))

    def _crop_roi(self, frame: np.ndarray, bbox: tuple) -> np.ndarray:
        """Crop the region of interest around the detected container bbox."""
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = bbox

        pad = self.roi_padding
        pw = x2 - x1
        ph = y2 - y1

        x1 = max(0, int(x1 - pw * pad.get("left", 0)))
        y1 = max(0, int(y1 - ph * pad.get("top", 0)))
        x2 = min(w, int(x2 + pw * pad.get("right", 0)))
        y2 = min(h, int(y2 + ph * pad.get("bottom", 0)))

        return frame[y1:y2, x1:x2]

    def _preprocess(self, crop: np.ndarray) -> np.ndarray:
        """Enhance cropped image for better OCR quality."""
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        # CLAHE for contrast enhancement
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        # Sharpen
        kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
        sharpened = cv2.filter2D(enhanced, -1, kernel)
        # Back to 3-channel for PaddleOCR
        return cv2.cvtColor(sharpened, cv2.COLOR_GRAY2BGR)

    def _parse_iso_number(self, text: str) -> Optional[tuple]:
        """Try to extract ISO 6346 container number from text."""
        match = ISO_PATTERN.search(text.upper().replace("O", "0").replace("I", "1"))
        if not match:
            return None
        raw = match.group(0).replace(" ", "").replace("-", "")
        # Restore alpha prefix — first 4 chars should be letters
        prefix = text.upper()[:4]
        if re.match(r"^[A-Z]{4}$", prefix):
            owner_code = prefix
        else:
            owner_code = raw[:4]
        digits = raw[4:]
        serial = digits[:6] if len(digits) >= 6 else digits
        check = digits[6] if len(digits) >= 7 else None
        iso = f"{owner_code}{serial}"
        if check:
            iso += check
        return owner_code, serial, check, iso

    def _parse_weights(self, lines: list[str]) -> dict:
        """Extract weight and capacity info from recognized lines."""
        result = {}
        full_text = "\n".join(lines)

        for match in WEIGHT_PATTERN.finditer(full_text):
            label = match.group(1).upper()
            value = match.group(2).strip().replace(" ", "")
            unit = match.group(3).upper()
            entry = f"{value} {unit}"

            if "TARE" in label:
                result["tare_weight"] = entry
            elif "NET" in label:
                result["net_weight"] = entry
            elif "PAYLOAD" in label:
                result["payload"] = entry
            elif "GROSS" in label:
                result["max_gross_weight"] = entry

        for match in CAPACITY_PATTERN.finditer(full_text):
            value = match.group(2).strip().replace(" ", "")
            unit = (match.group(3) or "CU.FT").upper()
            result["cube_capacity"] = f"{value} {unit}"

        return result

    def recognize(self, frame: np.ndarray, bbox: tuple) -> ContainerInfo:
        """Run OCR on a detected container region and parse results.

        Args:
            frame: Full video frame (BGR numpy array).
            bbox: Bounding box (x1, y1, x2, y2) of the detected container.

        Returns:
            ContainerInfo with parsed fields.
        """
        ocr_recognitions_total.inc()
        info = ContainerInfo()

        start = time.time()
        try:
            crop = self._crop_roi(frame, bbox)
            if crop.size == 0:
                return info

            processed = self._preprocess(crop)
            results = self.ocr.ocr(processed, cls=True)

            if not results or not results[0]:
                return info

            lines = []
            confidences = []
            for line in results[0]:
                text = line[1][0]
                conf = line[1][1]
                if conf >= self.confidence_threshold:
                    lines.append(text)
                    confidences.append(conf)

            info.raw_lines = lines
            if confidences:
                info.confidence = sum(confidences) / len(confidences)

            # Parse ISO number from all recognized text
            for line_text in lines:
                parsed = self._parse_iso_number(line_text)
                if parsed:
                    info.owner_code, info.serial_number, info.check_digit, info.iso_number = parsed
                    break

            # Parse weights
            weights = self._parse_weights(lines)
            info.max_gross_weight = weights.get("max_gross_weight")
            info.tare_weight = weights.get("tare_weight")
            info.net_weight = weights.get("net_weight")
            info.payload = weights.get("payload")
            info.cube_capacity = weights.get("cube_capacity")

            if info.is_valid():
                ocr_recognitions_success.inc()
                logger.info("Container recognized: %s (confidence=%.2f)",
                            info.iso_number, info.confidence)

        except Exception:
            logger.exception("OCR recognition failed")
        finally:
            elapsed = time.time() - start
            ocr_processing_duration.observe(elapsed)

        return info
