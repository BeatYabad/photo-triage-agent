"""Bound the entire pipe exchange, including blocked send or partial receive."""
import threading
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


NO_REQUEST=object()


class FileCopyTimeout(TimeoutError):
    """Only staging was written; no library destination was published."""


def exchange(connection, timeout, request=NO_REQUEST):
    completed=threading.Event()
    answer=[]
    def communicate():
        try:
            if request is not NO_REQUEST:
                connection.send(request)
            if not connection.poll(timeout):
                raise TimeoutError('File deadline exceeded; unfinished checks remain held')
            answer.append((True,connection.recv()))
        except BaseException as exc:
            answer.append((False,exc))
        finally:
            completed.set()
    # A blocked pipe receive must not keep the application alive after its
    # owning isolated worker is killed. Caller closes only that worker on error.
    threading.Thread(target=communicate,daemon=True,name='file-deadline-io').start()
    if not completed.wait(max(0,timeout)):
        raise TimeoutError('File deadline exceeded; unfinished checks remain held')
    success,value=answer[0]
    if not success:
        raise value
    return value


def copy_bytes(source, temporary, timeout=300):
    """Copy only to an exclusive staging path; timeout never publishes it."""
    from retention_policy import check_destination
    check_destination(temporary)
    try:
        # This child uses only the standard library. On Windows the virtualenv
        # executable is a launcher; use the real interpreter so timeout kills
        # the actual copying process and cannot leave its stdout pipe open.
        result=subprocess.run([getattr(sys,'_base_executable',sys.executable),str(Path(__file__).resolve()),
            '--copy',str(source),str(temporary)],capture_output=True,text=True,
            timeout=min(300,max(.001,timeout)),
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except subprocess.TimeoutExpired as exc:
        raise FileCopyTimeout('Five-minute copy deadline exceeded; original retained and staging held') from exc
    if result.returncode:
        raise OSError(result.stderr[-1500:])
    return json.loads(result.stdout)


def replace_cloud_placeholder(temporary,destination,expected,timeout=300):
    """Publish verified restore bytes without reading the cloud placeholder."""
    code='''import os,sys
from pathlib import Path
p=Path(sys.argv[2]);s=p.stat()
if (s.st_ino,s.st_size,s.st_mtime_ns)!=tuple(map(int,sys.argv[3:])) or not (getattr(s,'st_file_attributes',0)&0x441000):
 raise ValueError('Restore destination changed; original retained')
os.replace(sys.argv[1],p)
'''
    result=subprocess.run([getattr(sys,'_base_executable',sys.executable),'-c',code,
        str(temporary),str(destination),str(expected.st_ino),str(expected.st_size),str(expected.st_mtime_ns)],
        capture_output=True,text=True,timeout=min(300,timeout),
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise OSError(result.stderr[-1500:])


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--copy',nargs=2,required=True)
    args=parser.parse_args()
    digest=hashlib.sha256();length=0
    with open(args.copy[0],'rb') as source,open(args.copy[1],'xb') as destination:
        for chunk in iter(lambda:source.read(4*1024*1024),b''):
            digest.update(chunk);length+=len(chunk);destination.write(chunk)
        destination.flush();os.fsync(destination.fileno())
    print(json.dumps({'sha256':digest.hexdigest(),'bytes':length}))
