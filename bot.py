import cv2
import numpy as np
import pytesseract
from pyzbar.pyzbar import decode
from PIL import Image, ImageEnhance

def decode_barcode(image_path):
    """
    1) Try zbar (pyzbar) for standard barcodes.
    2) If that fails, do an advanced OpenCV + Tesseract pipeline:
       - Convert to grayscale
       - Deskew (attempt to correct rotation)
       - Morphological filtering
       - Increase contrast
       - Tesseract OCR
       - Regex for AZT pattern
    """

    # --- Attempt #1: pyzbar decode for normal barcodes ---
    try:
        pil_img = Image.open(image_path)
        raw_barcodes = decode(pil_img)
        if raw_barcodes:
            return raw_barcodes[0].data.decode('utf-8')
    except Exception as e:
        print("zbar decode error:", e)

    # --- Attempt #2: Tesseract with OpenCV preprocessing for 'AZT...' ---
    try:
        # Load via OpenCV for advanced ops
        img = cv2.imread(image_path)

        if img is None:
            print("OpenCV could not read image.")
            return None

        # Convert to grayscale
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # Deskew image (attempt to correct rotation)
        deskewed = deskew_image(gray)

        # Morphological filtering (remove noise, close gaps)
        # For example, a slight dilation or erosion
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2,2))
        morph = cv2.morphologyEx(deskewed, cv2.MORPH_CLOSE, kernel, iterations=1)

        # Increase contrast
        pil_morph = Image.fromarray(morph)
        enhancer = ImageEnhance.Contrast(pil_morph)
        high_contrast = enhancer.enhance(2.0)  # double contrast

        # Convert back to np.array for Tesseract
        final_img = np.array(high_contrast)

        # OCR
        text = pytesseract.image_to_string(final_img, lang='eng')

        # Search for 'AZT' pattern ignoring case
        import re
        match = re.search(r'(AZT\d+)', text.upper())
        if match:
            return match.group(1)

    except Exception as e:
        print("Tesseract OCR error:", e)

    return None


def deskew_image(gray):
    """
    Attempt to correct image rotation using OpenCV.
    Finds the largest text contour or overall skew angle,
    then rotates the image to deskew.
    """

    # Threshold
    blur = cv2.GaussianBlur(gray, (3,3), 0)
    _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Invert so text is black, background white
    # (depending on your images, might invert or might not)
    thresh_inv = 255 - thresh

    # Find contours
    contours, _ = cv2.findContours(thresh_inv, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return gray  # nothing to deskew

    # Find the largest contour
    largest_contour = max(contours, key=cv2.contourArea)

    # Get the minAreaRect (box) angle
    rect = cv2.minAreaRect(largest_contour)
    angle = rect[-1]

    # Adjust angle
    #  - if angle < -45, rotate by (angle + 90)
    #  - else rotate by angle
    if angle < -45:
        angle = angle + 90
    # minAreaRect angle is clockwise, so we invert
    angle = -angle

    # Rotate
    (h, w) = gray.shape[:2]
    center = (w//2, h//2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)

    return rotated
