"""Bounded fallback for attached artwork and inaccurate video endpoint metadata."""
from io import BytesIO
from bisect import bisect_right
from fractions import Fraction
import math
from pathlib import Path
import shutil
import subprocess
import hashlib
import json


def frame_selection(indices):
    """Keep expression depth logarithmic; FFmpeg rejects long left-deep sums."""
    terms=[f'eq(n\\,{i})' for i in sorted(set(indices))]
    def combine(items):
        if len(items)==1:return items[0]
        middle=len(items)//2
        return '('+combine(items[:middle])+'+'+combine(items[middle:])+')'
    return combine(terms) if terms else '0'


class FFmpegVideo:
    def __init__(self,path,config,identity=None):
        self.path=Path(path)
        configured=config.get('ffmpeg')
        bundled=Path(config.get('exiftool','exiftool.exe')).parent/'ffmpeg.exe'
        self.program=str(configured or (bundled if bundled.is_file() else shutil.which('ffmpeg') or ''))
        if not self.program:raise ValueError('FFmpeg is required to recover this video decoder failure')
        cache=None
        if identity and config.get('video_checkpoint_root'):
            # The caller freshly hashed this input. Cache only completed decode
            # metadata, never video frames, so bounded retries need not revalidate
            # the same stream before resuming saved classification samples.
            with Path(self.program).open('rb') as stream:decoder=hashlib.file_digest(stream,'sha256').hexdigest()
            signature={'identity':identity,'decoder':decoder,'code':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
            key=hashlib.sha256(json.dumps(signature,sort_keys=True).encode()).hexdigest()
            cache=Path(config['video_checkpoint_root'])/'verified-decode'/(key+'.json')
            if cache.exists():
                saved=json.loads(cache.read_text())
                if saved['signature']!=signature:raise ValueError('Video decode receipt identity differs')
                self.times=saved['times'];self.count=len(self.times);self.duration=saved['duration']
                if not self.times or not all(math.isfinite(t) and t>=0 for t in self.times) or not math.isfinite(self.duration) or self.duration<=self.times[-1] or any(b<a for a,b in zip(self.times,self.times[1:])):
                    raise ValueError('Invalid verified decode timeline')
                self.fps=self.count/self.duration
                self.decode_timeout=min(900,max(45,float(config.get('video_decode_timeout',900))))
                return
        # Full-stream validation can exceed a short metadata timeout on long
        # videos. The caller enforces the overall fifteen-minute video deadline.
        self.decode_timeout=min(900,max(45,float(config.get('video_decode_timeout',900))))
        # Uppercase V excludes cover art/attached pictures. Validate the complete
        # selected video stream before using corrected duration/frame metadata.
        # Decode linearly: seeking into a Live Photo's short HEVC GOP can lose
        # its reference frames. Passthrough avoids the null muxer's synthetic
        # duplicate-DTS errors. Frame records give the actual presentation
        # timeline, including variable frame rates and a nonzero start time.
        data=self.run(['-i',str(self.path),'-map','0:V:0','-an',
            '-vf','setpts=PTS-STARTPTS','-fps_mode','passthrough',
            '-f','framehash','-hash','adler32','pipe:1'])
        lines=data.decode().splitlines()
        base=next((Fraction(line.split(':',1)[1].strip()) for line in lines if line.startswith('#tb 0:')),None)
        records=[line.split(',') for line in lines if line.strip() and not line.startswith('#')]
        if base is None or not records:raise ValueError('FFmpeg did not verify a complete video stream')
        self.times=[float(int(record[2])*base) for record in records]
        self.count=len(records)
        self.duration=float((int(records[-1][2])+max(1,int(records[-1][3])))*base)
        if (not math.isfinite(self.duration) or self.duration<=0 or
                any(b<a for a,b in zip(self.times,self.times[1:]))):
            raise ValueError('FFmpeg did not verify a complete video stream')
        self.fps=self.count/self.duration
        if cache is not None:
            cache.parent.mkdir(parents=True,exist_ok=True)
            temporary=cache.with_suffix('.tmp')
            temporary.write_text(json.dumps({'signature':signature,'times':self.times,'duration':self.duration}))
            temporary.replace(cache)

    def run(self,args):
        result=subprocess.run([self.program,'-nostdin','-v','error','-xerror',*args],
                              capture_output=True,timeout=self.decode_timeout)
        if result.returncode or result.stderr.strip():
            raise ValueError('FFmpeg video decode failed: '+result.stderr.decode(errors='replace')[-1000:])
        return result.stdout

    def read(self,seconds,endpoint=False):
        from PIL import Image
        import numpy as np
        if not math.isfinite(seconds) or seconds<0 or seconds>self.duration:
            raise ValueError('Requested sample is outside the decoded timeline')
        index=self.count-1 if endpoint else max(0,bisect_right(self.times,seconds)-1)
        # Selecting a decoded frame number avoids timestamp seek ambiguity and
        # never starts midway through a dependent HEVC group of pictures.
        args=['-i',str(self.path),'-map','0:V:0','-vf',f'select=eq(n\\,{index})','-fps_mode','passthrough']
        data=self.run([*args,'-an','-frames:v','1','-f','image2pipe','-c:v','png','-compression_level','1','pipe:1'])
        if not data:raise ValueError('FFmpeg returned no frame for the requested sample')
        with Image.open(BytesIO(data)) as image:
            return np.asarray(image.convert('RGB'))[:,:,::-1].copy()


    def iter_samples(self, sample_times):
        """Decode selected native frames once, streaming full-size PNGs in RAM."""
        import struct
        import threading
        import numpy as np
        ordered=list(sample_times)
        indices=[]
        for seconds in ordered:
            if not math.isfinite(seconds) or not 0<=seconds<=self.duration:
                raise ValueError('Requested sample is outside the decoded timeline')
            index=self.count-1 if seconds>=self.duration-1/self.fps-1e-6 else max(0,bisect_right(self.times,seconds)-1)
            indices.append(index)
        if indices!=sorted(indices):raise ValueError('Samples must be ordered')
        if not indices:return
        expression=frame_selection(indices)
        command=[self.program,'-nostdin','-v','error','-xerror','-i',str(self.path),
            '-map','0:V:0','-vf','select='+expression,'-fps_mode','passthrough',
            '-an','-f','image2pipe','-c:v','png','-compression_level','1','pipe:1']
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        errors=bytearray()
        def drain():
            while True:
                block=process.stderr.read(4096)
                if not block:break
                if len(errors)<65536:errors.extend(block[:65536-len(errors)])
        thread=threading.Thread(target=drain,daemon=True);thread.start()
        def exact(size):
            pieces=[]
            while size:
                block=process.stdout.read(size)
                if not block:
                    thread.join(5)
                    detail=errors.decode(errors='replace')[-1000:]
                    raise ValueError('FFmpeg returned an incomplete sample'+(': '+detail if detail else ' (no decoder diagnostic)'))
                pieces.append(block);size-=len(block)
            return b''.join(pieces)
        try:
            previous=None;frame=None
            for seconds,index in zip(ordered,indices):
                if index!=previous:
                    signature=exact(8)
                    if signature!=b'\x89PNG\r\n\x1a\n':raise ValueError('Invalid FFmpeg sample stream')
                    parts=[signature];total=8
                    while True:
                        header=exact(8);size=struct.unpack('>I',header[:4])[0]
                        total+=size+12
                        if total>256*1024*1024:raise ValueError('Decoded sample exceeds memory limit')
                        parts.extend((header,exact(size+4)))
                        if header[4:]==b'IEND':break
                    from PIL import Image
                    with Image.open(BytesIO(b''.join(parts))) as image:
                        frame=np.asarray(image.convert('RGB'))[:,:,::-1].copy()
                    previous=index
                yield seconds,frame
            if process.stdout.read(1):raise ValueError('Unexpected extra FFmpeg sample')
            code=process.wait(timeout=45);thread.join(5)
            if thread.is_alive() or code or errors:
                raise ValueError('FFmpeg sample decode failed: '+errors.decode(errors='replace')[-1000:])
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=10)
            process.stdout.close();thread.join(5);process.stderr.close()


class SequentialCapture:
    """Decode forward once; retrieve every requested full-resolution sample."""
    def __init__(self, capture, fps, count):
        if not math.isfinite(fps) or fps <= 0 or not math.isfinite(count) or count < 1:
            raise ValueError('Invalid sequential video timeline')
        self.capture=capture;self.fps=fps;self.count=int(count)
        self.index=-1;self.frame=None

    def read(self, seconds, endpoint=False):
        if not math.isfinite(seconds) or seconds<0 or seconds>self.count/self.fps:
            raise ValueError('Requested sample is outside the decoded timeline')
        # OpenCV's FFmpeg seek rounds timestamp * FPS to the nearest frame.
        target=self.count-1 if endpoint else min(self.count-1,int(math.floor(seconds*self.fps+.5)))
        if target<self.index:raise ValueError('Sequential samples must be ordered')
        if target==self.index:return True,self.frame
        while self.index<target:
            if not self.capture.grab():return False,None
            self.index+=1
        ok,self.frame=self.capture.retrieve()
        return ok,self.frame


def endpoint_readable(path,count):
    """Check header endpoint before paying for model inference on a bad timeline."""
    import cv2
    probe=cv2.VideoCapture(str(path))
    try:
        if not probe.isOpened() or not math.isfinite(count) or count<1:return False
        probe.set(cv2.CAP_PROP_POS_FRAMES,int(count)-1)
        ok,frame=probe.read()
        return bool(ok and frame is not None)
    finally:probe.release()
