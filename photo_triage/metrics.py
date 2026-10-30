"""Shared decoded-image analyzers for the app and external pipelines."""
import cv2
import numpy as np
from PIL import Image


def laplacian_variance(gray):
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def quality(image, dimensions, frames=1, options=None):
    options = options or {'blur_threshold': 25., 'detail_threshold': 45., 'min_long_edge': 640}
    im = image.convert('RGB').copy()
    im.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
    gray = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2GRAY)
    focus = laplacian_variance(gray)
    tiles = [laplacian_variance(tile) for strip in np.array_split(gray, 3)
             for tile in np.array_split(strip, 3, axis=1) if min(tile.shape) >= 3]
    dark, bright = float((gray < 12).mean()), float((gray > 243).mean())
    reasons = (['nearly_all_dark'] if dark >= .98 else []) + (['nearly_all_bright'] if bright >= .98 else [])
    return {'status': 'analyzed', 'candidate': bool(reasons),
            'category': 'quality' if reasons else None, 'reasons': reasons,
            'dimensions': dimensions, 'frames': frames, 'thresholds': options,
            'metrics': {'laplacian_variance': round(focus, 3),
                        'tile_detail_p75': round(float(np.percentile(tiles, 75)) if tiles else focus, 3),
                        'sharpest_tile_detail': round(max(tiles, default=focus), 3),
                        'dark_fraction': round(dark, 5), 'bright_fraction': round(bright, 5)},
            'holds': (['visual_review_required'] if reasons else []) +
                     (['only_first_frame_screened'] if frames > 1 else []),
            'limitation': 'Blur is diagnostic only; technical scores do not establish personal value'}


def signature(image):
    """Legacy-compatible 64-bit dHash; similarity is never exact identity."""
    a = np.asarray(image.convert('L').resize((9, 8), Image.Resampling.LANCZOS))
    return sum(int(v) << i for i, v in enumerate((a[:, 1:] > a[:, :-1]).reshape(-1)))


def perceptual_hashes(image, hash_size=16):
    import imagehash
    return {name: str(fn(image, hash_size=hash_size)) for name, fn in
            [('phash', imagehash.phash), ('dhash', imagehash.dhash),
             ('ahash', imagehash.average_hash), ('whash', imagehash.whash)]}
