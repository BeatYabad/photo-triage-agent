"""Explainable local image-quality candidates; human value is never inferred."""
import hashlib
import json

DEFAULTS = {'blur_threshold':25.0, 'detail_threshold':45.0, 'min_long_edge':640}

def rule(options):
    import cv2
    from PIL import __version__
    return 'photo-triage-quality-v1-'+__import__('triage_adapter').fingerprint(False)+'-'+hashlib.sha256(json.dumps(
        [options, cv2.__version__, __version__],sort_keys=True).encode()).hexdigest()

def analyze(path, options=None):
    from PIL import Image, ImageOps
    with Image.open(path) as source:
        dimensions=list(source.size); frames=getattr(source,'n_frames',1)
        image=ImageOps.exif_transpose(source).convert('RGB')
        return analyze_image(image, dimensions, frames, options)

def analyze_image(image, dimensions, frames=1, options=None):
    """Analyze a shared decoded RGB image without opening the source again."""
    from photo_triage.metrics import quality
    return quality(image, dimensions, frames, options)


def inspect(row, options, exclusions, cache, version):
    from nsfw_analysis import inspect as common
    from pathlib import Path
    from PIL import UnidentifiedImageError
    def decode(path,settings):
        try:return analyze(path,settings)
        except (UnidentifiedImageError,OSError) as exc:
            # RAW/HEIC support failures stay held; they are not evidence of damage.
            if Path(path).suffix.lower() not in {'.jpg','.jpeg','.jpe','.png','.bmp','.webp','.tif','.tiff'}:raise
            return {'status':'analyzed','candidate':True,'category':'quality','reasons':['decoder_rejected_image'],
                    'error':str(exc),'holds':['visual_review_required','decode_failure_does_not_prove_corruption']}
    return common(row,options,exclusions,cache,version,analyzer=decode)
