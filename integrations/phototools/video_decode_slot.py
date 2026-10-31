"""One native video decode/screening lifetime across classifier processes."""
import ctypes
from ctypes import wintypes as w
from contextlib import contextmanager
from functools import wraps

@contextmanager
def video_slot(timeout_ms=240000):
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.CreateMutexW.argtypes=[ctypes.c_void_p,w.BOOL,w.LPCWSTR];k.CreateMutexW.restype=w.HANDLE
    k.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD]
    k.ReleaseMutex.argtypes=[w.HANDLE];k.CloseHandle.argtypes=[w.HANDLE]
    handle=k.CreateMutexW(None,False,'Local\\PhotoToolsVideoDecodeSlot')
    if not handle:raise ctypes.WinError(ctypes.get_last_error())
    acquired=False
    try:
        result=k.WaitForSingleObject(handle,timeout_ms)
        if result not in (0,0x80):raise TimeoutError('Video decode slot wait timed out; checks remain pending')
        acquired=True;yield
    finally:
        if acquired:k.ReleaseMutex(handle)
        k.CloseHandle(handle)

def serialized_video(function):
    @wraps(function)
    def wrapped(*args,**kwargs):
        # Hold through streamed inference: its FFmpeg process stays alive until
        # the generator closes. Windows mutexes allow same-thread fallback recursion.
        config=getattr(args[0],'config',{}) if args else {}
        limit=max(1,min(float(config.get('video_file_timeout',900)),900))
        with video_slot(timeout_ms=int(limit*1000)):return function(*args,**kwargs)
    return wrapped
