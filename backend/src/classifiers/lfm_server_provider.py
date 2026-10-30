"""Persistent server alternative to launching a new CLI/model per photo."""
from PIL import Image, ImageOps
from photo_triage.vision import VisionProvider, junk_result
from .base import ClassificationResult, ImageCategory, ModelProvider


class LFMServerProvider(ModelProvider):
    name = 'lfm2.5-vl-server'

    def __init__(self, endpoint='http://127.0.0.1:18766/v1/chat/completions', model='LFM2.5-VL-1.6B-Q8_0'):
        self.provider = VisionProvider(endpoint, model=model)

    def classify(self, image_path):
        with Image.open(image_path) as image:
            result = self.provider.inspect(ImageOps.exif_transpose(image))
        checked=junk_result(result,[0,0])
        if checked['status']=='needs_review':
            raise ValueError('Uncertain triage result: '+result['observation'])
        mapping = {'meme_or_cartoon': ImageCategory.MEME,
                   'screenshot': ImageCategory.SCREENSHOT,
                   'receipt': ImageCategory.DOCUMENT,
                   'personal_document': ImageCategory.DOCUMENT}
        # Zero is explicitly uncalibrated, not a fabricated probability of 0.8.
        return ClassificationResult(category=mapping.get(result['category'], ImageCategory.OTHER),
                                    confidence=0., contains_faces=False,
                                    is_screenshot=result['category'] == 'screenshot',
                                    is_meme=result['category'] == 'meme_or_cartoon',
                                    description=result['observation'])

    def classify_batch(self, image_paths):
        return [self.classify(path) for path in image_paths]

    def health_check(self):
        from photo_triage.runtime import healthy
        return healthy()
