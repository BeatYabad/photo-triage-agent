"""Conservative recompression duplicate detection; never compare different galleries for removal."""
import hashlib
import json
from collections import defaultdict
from pathlib import Path

POLICY='same-frame-recompression-v1'

def image(path):
    from PIL import Image, ImageOps
    import pillow_heif
    pillow_heif.register_heif_opener()
    with Image.open(path) as im:
        if getattr(im,'n_frames',1)!=1:raise ValueError('Animated/multi-page image held')
        if 'A' in im.getbands() or 'transparency' in im.info:raise ValueError('Transparency requires separate comparison')
        if im.width*im.height>80_000_000:raise ValueError('Image size limit')
        result=ImageOps.exif_transpose(im).convert('RGB')
        tables=getattr(im,'quantization',{})
        quant=sum(sum(t) for t in tables.values())/sum(map(len,tables.values())) if tables else 0
        # Actual codec, not the extension. Lossless outranks JPEG at equal resolution.
        quality=(result.width*result.height,int(im.format!='JPEG'),-quant)
        return result,quality

def signature(im):
    from photo_triage.metrics import signature as core_signature
    return core_signature(im)


def agreement(a,b):
    """Same dimensions and extremely close full-frame pixels; no crops or resized edits."""
    import numpy as np
    if a.size!=b.size:return None
    if a.info.get('icc_profile')!=b.info.get('icc_profile'):return None
    x=np.asarray(a,dtype=np.float32);y=np.asarray(b,dtype=np.float32)
    if min(float(x.std()),float(y.std()))<10:return None
    delta=np.abs(x-y)
    mae=float(delta.mean());changed=float((delta.max(axis=2)>24).mean())
    # A local edit can disappear in a global average; inspect every 32px tile too.
    local=max(float(delta[j:j+32,i:i+32].mean()) for j in range(0,a.height,32) for i in range(0,a.width,32))
    if mae>1.5 or changed>.002 or local>4:return None
    return {'policy':POLICY,'mean_pixel_error':mae,'changed_pixel_fraction':changed,'max_tile_error':local}

def find(rows,out):
    """One decoder at a time; index hashes, then verify candidate pixels in full."""
    buckets=defaultdict(list);features={};plans=[];issues=[];removed=set()
    eligible=[r for r in rows if r.get('kind')=='photo' and r.get('status')=='verified_local_bytes' and Path(r['path']).exists()]
    for number,row in enumerate(eligible,1):
        try:
            identity=(row['sha256'],row['bytes'])
            if identity not in features:
                im,quality=image(row['path']);features[identity]=(im.size,signature(im),quality)
            size,code,quality=features[identity]
            candidates={i for band in range(4) for i in buckets[(row['library'],size,band,(code>>(16*band))&65535)]}
            for index in sorted(candidates):
                other=eligible[index]
                if other['path'] in removed or row['path'] in removed or other['sha256']==row['sha256']:continue
                oid=(other['sha256'],other['bytes']);_,oldcode,oq=features[oid]
                if (code^oldcode).bit_count()>3:continue
                a,_=image(row['path']);b,_=image(other['path']);evidence=agreement(a,b)
                if evidence is None:continue
                # Stable tie-break only after resolution/codec/quantization ranking.
                winner,loser=(row,other) if (quality,row['path'])>(oq,other['path']) else (other,row)
                plans.append({'source':loser,'keeper':winner,'evidence':evidence,
                              'source_quality':features[(loser['sha256'],loser['bytes'])][2],
                              'keeper_quality':features[(winner['sha256'],winner['bytes'])][2]})
                removed.add(loser['path'])
            for band in range(4):buckets[(row['library'],size,band,(code>>(16*band))&65535)].append(number-1)
        except Exception as exc:issues.append({'path':row['path'],'error':str(exc)})
        if number%250==0:print(json.dumps({'phase':'visual_comparison','processed':number,'total':len(eligible),'matches':len(plans)}),flush=True)
    # If a better keeper superseded an earlier winner, compare to the final winner again.
    by_source={p['source']['path']:p for p in plans}
    for plan in plans:
        while plan['keeper']['path'] in by_source:plan['keeper']=by_source[plan['keeper']['path']]['keeper']
    return plans,issues

def apply(plans,rows,config,out,reader,recycler):
    if not config.get('duplicate_cleanup_enabled',True):return {'recycled':0,'held':[],'disabled':True}
    import two_library_duplicates as exact
    import sync_local_photo_folders as sync
    from live_photo_components import embedded_hold
    from privacy_storage import copy_verified,verify
    from photo_names import choose
    import merge_photos as media
    result={'recycled':0,'held':[]};roots=config['roots'];journal=out/'visual-actions.jsonl'
    wanted,deleted=exact.settings.decisions(config['safe_ledgers'],config['deletion_ledger'])
    protected=exact.settings.review_move_hashes(config)|deleted
    for plan in plans:
        source=plan['source'];keeper=plan['keeper']
        try:
            if source['library']!=keeper['library']:raise ValueError('Cross-gallery removal forbidden')
            if {source['sha256'],keeper['sha256']} & protected:raise ValueError('Review/deletion decision held')
            # Keep decisions protect this distinct encoding from visual retirement.
            if source['sha256'] in wanted:raise ValueError('Explicit KEEP encoding preserved')
            exact.ensure_digikam_closed()
            for row in (source,keeper):
                exact.verify_row(reader,row,roots)
                hold=exact.companion_hold(row,rows) or embedded_hold(Path(row['path']),config['exiftool'])
                if row.get('native_live_photo') or hold:raise ValueError(hold or 'Complete Live Photo preserved')
            evidence=exact.asset_evidence(source,keeper,config.get('icloud_database'),config.get('delivery_ledger')) if source['library']=='icloud' else None
            a,_=image(source['path']);b,_=image(keeper['path'])
            if agreement(a,b) is None:raise ValueError('Visual agreement changed')
            backup_root=config['preservation']/'visual-duplicates'
            backup=choose(backup_root,Path(source['path']).name,source['sha256'],source['bytes'])
            copy_verified(Path(source['path']),backup,backup_root,source['sha256'],source['bytes'])
            if backup.samefile(source['path']) or backup.samefile(keeper['path']):raise ValueError('Independent preservation required')
            exact.verify_row(reader,source,roots);exact.verify_row(reader,keeper,roots)
            verify(backup,source['sha256'],source['bytes'])
            if source['library']=='icloud' and exact.asset_evidence(source,keeper,config.get('icloud_database'),config.get('delivery_ledger'))!=evidence:raise ValueError('Native asset changed')
            event={**plan,'backup':str(backup),'asset_evidence':evidence}
            media.journal(journal,{'action':'visual_recycle_intent',**event})
            recycler(source['path'])
            if Path(source['path']).exists():raise ValueError('Source recycling incomplete')
            exact.verify_row(reader,keeper,roots);verify(backup,source['sha256'],source['bytes'])
            media.journal(journal,{'action':'visual_recycled_verified',**event})
            result['recycled']+=1
        except Exception as exc:
            result['held'].append({'path':source['path'],'error':str(exc)})
    return result
