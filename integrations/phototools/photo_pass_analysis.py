"""Shared decoders and lazily loaded, offline models for the unified pass."""

import hashlib

import importlib.metadata

import json

import time

from pathlib import Path
from video_decode_slot import serialized_video



HERE=Path(__file__).resolve().parent

IMAGE_CHECKS={'quality','junk','nsfw','tiktok'}



def failure(exc):

    return {'status':'error','candidate':False,'error':type(exc).__name__+': '+str(exc),

            'holds':['check_incomplete']}





def multi_frame_result(assessments,frames):

    if len(assessments)!=frames or [r['frame_index'] for r in assessments]!=list(range(frames)):

        raise ValueError('Every image frame must be checked')

    uncertain=any(r['status']=='needs_review' for r in assessments)

    if any(r['status'] not in {'analyzed','needs_review'} for r in assessments):

        raise ValueError('An image frame check failed')

    candidate=any(r.get('candidate') for r in assessments)

    return {'status':'needs_review' if uncertain else 'analyzed','candidate':candidate,

            'category':'nsfw' if candidate else None,'frames':frames,'frame_evidence':assessments,

            'holds':['sexual_content_unconfirmed'] if uncertain else []}



class Analyzer:

    def __init__(self, config):

        self.config=config;self.models={};self.versions={};self.timings={}

        from inference_backend import activate, profile

        self.inference=activate(profile(config))



    def close(self):

        if 'metadata_reader' in self.models:self.models['metadata_reader'].close()



    def rule(self, check):

        if check in self.versions:return self.versions[check]

        files=['photo_pass_analysis.py','photo_pass.py','video_decode.py','video_filter_review.py','video_checkpoints.py','mpo_frames.py','inference_backend.py']

        packages=['Pillow','pillow-heif','rawpy']

        if check=='quality':files+=['quality_analysis.py'];packages+=['opencv-python-headless','numpy']

        if check=='junk':files+=['triage_adapter.py','local_vision_server.py']

        if check=='nsfw':files+=['nsfw_analysis.py']

        if check=='tiktok':files+=['tiktok_video_triage.py'];packages+=['rapidocr','opencv-python-headless']

        if check=='metadata':files+=['digikam_metadata.py','live_photo_cleanup.py']

        digest=hashlib.sha256()

        paths=[HERE/name for name in files]

        if check in {'junk','quality'}:
            from triage_adapter import fingerprint
            digest.update(fingerprint(check=='junk').encode())
            paths.append(HERE/'triage_adapter.py')

        if check=='nsfw':

            import nsfw_analysis as nsfw

            from photo_settings import nsfw_options

            digest.update(self.rule('junk').encode())

            digest.update(nsfw.rule(*nsfw_options(self.config),config=self.config).encode())

            digest.update(self.config.get('nsfw_device','cpu').encode())

        if check=='metadata':paths.append(self.config['exiftool'])

        if check=='tiktok':

            import rapidocr

            modeldir=Path(rapidocr.__file__).parent/'models'

            paths += [modeldir/name for name in ('PP-OCRv6_det_small.onnx','PP-OCRv6_rec_small.onnx','ch_ppocr_mobile_v2.0_cls_mobile.onnx')]

            files=['archive_text_images.py'];paths += [HERE/name for name in files]

        for path in paths:

            digest.update(path.name.encode())

            from operational_rule_hash import rule_file_digest
            digest.update(rule_file_digest(path))

        for name in packages:

            try:version=importlib.metadata.version(name)

            except importlib.metadata.PackageNotFoundError:version='missing'

            digest.update((name+version).encode())

        digest.update(json.dumps(self.config.get('quality_options',{}),sort_keys=True).encode())

        digest.update(json.dumps(self.inference,sort_keys=True).encode())

        value='unified-pass-v1-'+digest.hexdigest();self.versions[check]=value

        return value



    def model(self, name):

        if name not in self.models:

            if name=='clip':

                import numpy as np

                import scan_reference_photos as scan

                if not self.config.get('text_embeddings_prepared'):

                    scan.prepare_text(self.config['clip_model'],self.config.get('dependencies',[]))

                from inference_backend import ClipSession

                self.models[name]=(ClipSession(self.config['clip_model']/'vision_model_quantized.onnx',self.inference),

                                   np.load(self.config['clip_model']/'reference-text-embeddings.npy'))

            elif name=='nsfw':

                import nsfw_analysis as nsfw

                from photo_settings import nsfw_options

                self.models[name]=nsfw.create_detector(*nsfw_options(self.config),

                    threads=self.config.get('model_threads',2),device=self.config.get('nsfw_device','cpu'),config=self.config)

                if hasattr(self.models[name],'set_context_encoder'):

                    self.models[name].set_context_encoder(lambda:self.model('clip')[0])

            elif name=='ocr':

                import archive_text_images as ocr

                ocr.initialize(threads=self.config.get('model_threads',2),inference=self.inference);self.models[name]=ocr.LOCAL.engine

        return self.models[name]



    def ocr(self, image):

        import numpy as np

        image=image.copy();image.thumbnail((960,960))

        started=time.perf_counter()

        result=self.model('ocr')(np.asarray(image)[:,:,::-1].copy())

        self.timings['ocr']=self.timings.get('ocr',0)+time.perf_counter()-started

        lines=[] if result.txts is None else list(zip(result.txts,result.scores,result.boxes))

        return lines,image.size



    def metadata(self, path):

        from digikam_metadata import read, portable

        from live_photo_cleanup import content_identifier

        if self.config.get('persistent_metadata',False):

            from exiftool_session import ExifToolSession

            if 'metadata_reader' not in self.models:self.models['metadata_reader']=ExifToolSession(self.config['exiftool'])

            raw=self.models['metadata_reader'].read(path)

        else:raw=read(path,self.config['exiftool'])

        if raw.get('Error'):raise ValueError(raw['Error'])

        return {'status':'cataloged','embedded':portable(raw),'content_identifier':content_identifier(raw),

                'live_photo_video_index':next((v for k,v in raw.items() if k.endswith(':LivePhotoVideoIndex')),None)}



    def image(self, path, checks):

        """Open/decode once, independently run every requested missing check."""

        from PIL import Image, ImageOps

        import pillow_heif

        pillow_heif.register_heif_opener()

        decode_started=time.perf_counter()

        try:

            with Image.open(path) as source:

                dimensions=list(source.size);frames=getattr(source,'n_frames',1)

                exif=source.getexif();make=str(exif.get(271,''));model=str(exif.get(272,''))

                image=ImageOps.exif_transpose(source).convert('RGB')

        except Image.UnidentifiedImageError as exc:

            if Path(path).suffix.lower()!='.orf':return {check:failure(exc) for check in checks}

            try:

                import rawpy

                # Decode the actual camera RAW in RAM; no previews or exports.

                with rawpy.imread(str(path)) as raw:

                    image=Image.fromarray(raw.postprocess(use_camera_wb=True))

                dimensions=list(image.size);frames=1;make='';model=''

            except Exception as raw_error:return {check:failure(raw_error) for check in checks}

        except Exception as exc:return {check:failure(exc) for check in checks}

        finally:self.timings['decode']=self.timings.get('decode',0)+time.perf_counter()-decode_started

        results=self.decoded_image(image,checks,dimensions,frames,make,model)

        if frames>1 and 'nsfw' in checks and self.config.get('nsfw_visual_review') and results['nsfw'].get('status')!='not_applicable':

            import nsfw_analysis

            assessments=[]

            try:

                with Image.open(path) as source:

                    if source.format=='MPO':

                        from mpo_frames import decode

                        images=decode(path)

                        if len(images)!=frames:raise ValueError('MPO resource count changed')

                        for index,frame in enumerate(images):

                            assessed=nsfw_analysis.analyze_image(frame,self.model('nsfw'),1)

                            assessments.append({'frame_index':index,**assessed})

                    else:

                        for index in range(frames):

                            source.seek(index)

                            assessed=nsfw_analysis.analyze_image(ImageOps.exif_transpose(source).convert('RGB'),self.model('nsfw'),1)

                            assessments.append({'frame_index':index,**assessed})

                results['nsfw']=multi_frame_result(assessments,frames)

            except Exception as exc:results['nsfw']=failure(exc)

        return results



    def decoded_image(self,image,checks,dimensions,frames=1,make='',model=''):

        """Apply image policies to an in-memory image or actual video frame."""

        results={};lines=None;ocr_error=None;ocr_size=None

        if 'tiktok' in checks:

            try:lines,ocr_size=self.ocr(image)

            except Exception as exc:ocr_error=failure(exc)

        for check in sorted(checks, key=lambda value: ({'junk':0,'tiktok':1,'nsfw':2,'quality':3}.get(value,4),value)):

            started=time.perf_counter()

            try:

                if check=='quality':

                    import quality_analysis as quality

                    if any(results.get(name,{}).get('candidate') for name in ('junk','tiktok')):

                        result={'status':'not_applicable','candidate':False,'reason':'junk_nominated_before_quality'}

                    else:

                        result=quality.analyze_image(image,dimensions,frames,self.config.get('quality_options'))

                elif check=='nsfw':

                    import nsfw_analysis as nsfw

                    if results.get('junk',{}).get('candidate'):

                        result={'status':'not_applicable','candidate':False,'reason':'supported_junk_before_nsfw'}

                    elif results.get('tiktok',{}).get('candidate'):

                        result={'status':'not_applicable','candidate':False,'reason':'supported_tiktok_before_nsfw'}

                    else:

                        result=nsfw.analyze_image(image,self.model('nsfw'),frames)

                elif check=='junk':
                    from triage_adapter import classify
                    result=classify(image,dimensions,frames)

                elif check=='tiktok':

                    if results.get('junk',{}).get('candidate'):

                        result={'status':'not_applicable','candidate':False,'reason':'supported_junk_before_tiktok'}

                    elif ocr_error:result=ocr_error.copy()

                    else:

                        from tiktok_video_triage import frame_features

                        evidence=frame_features(lines,*ocr_size)

                        result={'status':'analyzed','candidate':evidence['tiktok_text'],

                                'category':'tiktok' if evidence['tiktok_text'] else None,

                                'frame_evidence':[evidence],'holds':['visual_review_required'] if evidence['tiktok_text'] else []}

                else:raise ValueError('Unknown image check')

                if frames>1:result['holds']=sorted(set(result.get('holds',[])+['only_first_frame_screened']))

                results[check]=result

            except Exception as exc:results[check]=failure(exc)

            self.timings[check]=self.timings.get(check,0)+time.perf_counter()-started

        if frames==1 and checks & {'junk','quality'}:
            try:
                from photo_triage.metrics import perceptual_hashes
                signature=perceptual_hashes(image)['phash']
                for name in checks & {'junk','quality'}:
                    results[name]['visual_similarity']={'phash':signature,'bits':256,'engine':'photo_triage_agent'}
            except Exception as exc:
                for name in checks & {'junk','quality'}:
                    results[name]['similarity_error']=str(exc)
                    if results[name].get('status')=='analyzed':results[name]['status']='analyzed_with_check_gaps'
        return results



    @serialized_video
    def video(self, path, checks, force_ffmpeg=False):

        """One capture; union of NSFW and TikTok sample times; decoded frames shared."""

        import cv2

        import nsfw_analysis as nsfw

        import tiktok_video_triage as tiktok

        from PIL import Image

        results={}

        active=checks & IMAGE_CHECKS

        if not active:return results

        force_ffmpeg=force_ffmpeg or self.config.get('video_force_ffmpeg',False)

        video=cv2.VideoCapture() if force_ffmpeg else cv2.VideoCapture(str(path));fallback=None

        try:

            fps=video.get(cv2.CAP_PROP_FPS);count=video.get(cv2.CAP_PROP_FRAME_COUNT)

            duration=count/fps if fps else 0

            from video_decode import endpoint_readable

            if force_ffmpeg or path.suffix.lower()=='.qt' or not video.isOpened() or fps>240 or fps<=0 or count<=0 or not endpoint_readable(path,count):

                from video_decode import FFmpegVideo

                fallback=FFmpegVideo(path,self.config,getattr(self,'current_identity',None))

                fps,count,duration=fallback.fps,fallback.count,fallback.duration

            review_times=set(nsfw.samples(duration,fps)) if active & {'nsfw','junk','quality'} else set()

            nsfw_times=review_times if 'nsfw' in active else set()

            # Validate duration even if only TikTok was selected.

            if not duration>0 or not fps>0:raise ValueError('Unknown video duration or frame rate')

            tik_times={max(0,min(duration-1/fps,duration*f)) for f in tiktok.DEEP_SAMPLES} if 'tiktok' in active else set()

            meta=None;errors={};flagged=[];uncertain=[];evidence=[];read={check:0 for check in active};failed={check:[] for check in active}

            if active & {'tiktok','junk'}:

                try:meta=tiktok.metadata(path)

                except Exception as exc:

                    for check in active & {'tiktok','junk'}:errors[check]=failure(exc)

            from video_decode import SequentialCapture

            sequential=None if fallback else SequentialCapture(video,fps,count)

            recovered=[];dimensions=None

            frame_checks=active & {'junk','quality'}

            frame_results={check:[] for check in frame_checks}

            sample_times=sorted(review_times|tik_times)

            checkpoint=None;completed=[]

            if self.config.get('video_checkpoint_root') and getattr(self,'current_identity',None):

                from video_checkpoints import VideoCheckpoint

                checkpoint=VideoCheckpoint(self.config['video_checkpoint_root'],self.current_identity,

                    {c:self.rule(c) for c in active},sample_times)

                saved=checkpoint.load()

                if saved:

                    completed=saved['completed'];frame_results=saved['frame_results'];read=saved['read']

                    flagged=saved['flagged'];uncertain=saved['uncertain'];evidence=saved['evidence']

                    recovered=saved['recovered'];dimensions=saved['dimensions'];errors=saved['errors']

            remaining=[s for s in sample_times if s not in set(completed)]

            frames=fallback.iter_samples(remaining) if fallback else ((s,None) for s in remaining)

            for seconds,decoded in frames:

                decode_started=time.perf_counter()

                endpoint=seconds>=duration-1/fps-1e-6

                if fallback:

                    frame=decoded;ok=True;recovered.append(seconds)

                else:

                    ok,frame=sequential.read(seconds,endpoint)

                if not ok:

                    # Rebuild ALL sample positions from the corrected timeline.

                    # Mixing a header's old duration with the fallback caused

                    # false missing-frame failures at the end of short videos.

                    if force_ffmpeg:raise ValueError('Video decoder returned no frame')

                    return self.video(path,checks,force_ffmpeg=True)

                if ok:dimensions=(frame.shape[1],frame.shape[0])

                self.timings['decode']=self.timings.get('decode',0)+time.perf_counter()-decode_started

                assessed_frames={}

                if seconds in review_times and frame_checks:

                    assessed_frames=self.decoded_image(Image.fromarray(cv2.cvtColor(frame,cv2.COLOR_BGR2RGB)),frame_checks,list(dimensions),make='camera' if meta and meta.get('camera_metadata') else '')

                    for check,result in assessed_frames.items():

                        frame_results[check].append({'seconds':seconds,**result})

                        read[check]+=1

                for check,times in (('nsfw',nsfw_times),('tiktok',tik_times)):

                    if seconds not in times or check in errors:continue

                    try:

                        if check=='nsfw':

                            if assessed_frames.get('junk',{}).get('candidate'):

                                read[check]+=1

                                continue

                            inference_started=time.perf_counter()

                            assessed=nsfw.frame_result(self.model('nsfw'),frame)

                            self.timings['nsfw']=self.timings.get('nsfw',0)+time.perf_counter()-inference_started

                            if assessed['candidate']:flagged.append({'seconds':seconds,'detections':assessed['detections'],'scene':assessed.get('scene')})

                            elif assessed['status']=='needs_review':uncertain.append({'seconds':seconds,'scene':assessed.get('scene')})

                        else:

                            lines,size=self.ocr(Image.fromarray(cv2.cvtColor(frame,cv2.COLOR_BGR2RGB)))

                            evidence.append({'seconds':seconds,**tiktok.frame_features(lines,*size)})

                        read[check]+=1

                    except Exception as exc:errors[check]=failure(exc)

                completed.append(seconds)

                if checkpoint and not errors:

                    checkpoint.save(dict(completed=completed,frame_results=frame_results,read=read,

                        flagged=flagged,uncertain=uncertain,evidence=evidence,recovered=recovered,

                        dimensions=dimensions,errors=errors))

            for check in active:

                if check in errors:results[check]=errors[check];continue

                if check in frame_checks:

                    from video_filter_review import aggregate

                    results[check]=aggregate(check,frame_results[check],len(review_times),duration)

                    continue

                if check in errors:results[check]=errors[check];continue

                if not read[check]:results[check]=failure(ValueError('No video samples readable'));continue

                result={'status':'analyzed_with_sample_gaps' if failed[check] else 'analyzed',

                        'samples_read':read[check],'failed_sample_seconds':failed[check],'duration_seconds':duration,

                        'recovered_sample_seconds':[s for s in recovered if s in (nsfw_times if check=='nsfw' else tik_times)]}

                if check=='nsfw':

                    result.update(**nsfw.video_result(flagged,uncertain),

                                  samples_requested=len(nsfw_times),limitation='One-second samples plus midpoint/end; audio and other frames unscreened')

                    if uncertain and not flagged and not failed[check]:result['status']='needs_review'

                else:

                    verdict,cues=tiktok.verdict(meta,evidence,*dimensions,duration)

                    candidate=verdict in {'probable-download-needs-visual-review','ambiguous-platform-evidence'}

                    result.update(candidate=candidate,category='tiktok' if candidate else None,verdict=verdict,

                                  metadata=meta,frame_evidence=evidence,cues=cues,samples_requested=len(tik_times),

                                  limitation='Six near-start/middle/end samples; cropped/absent watermarks can escape detection')

                result['holds']=sorted(set(result.get('holds',[])+(['visual_review_required'] if check!='nsfw' and result['candidate'] else [])+(['sample_gaps'] if failed[check] else [])))

                results[check]=result

            return results

        except Exception as exc:

            return {**results,**{check:failure(exc) for check in active}}

        finally:video.release()

