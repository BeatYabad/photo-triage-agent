"""Verified non-overwriting copies for privacy handoffs; no gallery imports."""
import os
from pathlib import Path
import shutil
import uuid

import merge_photos as media
import review_queue as queue


def verify(path,digest,size):
    actual=queue.fresh(path)
    if (actual['sha256'],actual['bytes'])!=(digest,size):raise ValueError('Privacy handoff bytes differ')
    return actual


def cloud_placeholder(path):
    from scan_reference_photos import NONRESIDENT
    return Path(path).exists() and bool(getattr(Path(path).stat(),'st_file_attributes',0)&NONRESIDENT)


def copy_verified(source,destination,root,digest,size,check=lambda:None,restore_placeholder=False):
    from retention_policy import check_destination
    check_destination(destination)
    source=Path(source);destination=Path(destination)
    check();media.inside(destination,root);before=verify(source,digest,size)
    replace_cloud=restore_placeholder and cloud_placeholder(destination)
    expected=destination.stat() if replace_cloud else None
    if destination.exists() and not replace_cloud:verify(destination,digest,size);check();return destination
    destination.parent.mkdir(parents=True,exist_ok=True)
    check();media.inside(destination,root)
    media.space(destination.parent,size,1024**3)
    temporary=destination.with_name('.'+uuid.uuid4().hex+'.partial')
    from file_deadline import copy_bytes
    copied=copy_bytes(source,temporary)
    if (copied['sha256'],copied['bytes'])!=(digest,size):raise ValueError('Privacy copy bytes differ')
    verify(temporary,digest,size)
    if verify(source,digest,size)!=before:raise ValueError('Privacy source changed during copying')
    check();media.inside(destination,root)
    if replace_cloud:
        from file_deadline import replace_cloud_placeholder
        replace_cloud_placeholder(temporary,destination,expected)
    elif os.name=='nt':temporary.rename(destination)  # Windows rename never replaces.
    else:os.link(temporary,destination);temporary.unlink()  # Fixture/platform parity.
    verify(destination,digest,size);check();return destination
