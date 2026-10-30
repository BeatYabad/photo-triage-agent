"""Create numbered local candidate contact sheets; never modify original photos."""
import argparse
import json
from pathlib import Path
import sys
import time

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--scan',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--dependencies',action='append',type=Path,default=[])
    p.add_argument('--limit',type=int,default=96)
    p.add_argument('--minimum-score',type=float,default=.27)
    p.add_argument('--input',type=Path)
    p.add_argument('--exclude-mapping',type=Path,action='append',default=[])
    p.add_argument('--exclusions',type=Path,action='append',required=True)
    a=p.parse_args()
    for dependency in a.dependencies:sys.path.insert(0,str(dependency))
    from PIL import Image,ImageOps,ImageDraw
    import pillow_heif
    pillow_heif.register_heif_opener()
    from photo_settings import hashes
    excluded=hashes(a.exclusions)
    for path in a.exclude_mapping:
        excluded.update(row['sha256'] for row in json.loads(path.read_text(encoding='utf-8')))
    unique={}
    for line in (a.input or a.scan/'candidates.jsonl').read_text(encoding='utf-8').splitlines():
        try: row=json.loads(line)
        except json.JSONDecodeError:continue
        if row.get('contextual_evidence'):
            if not row.get('candidate'):continue
        elif row.get('target_score',0)<a.minimum_score:continue
        key=row['sha256']
        if key in excluded:continue
        if key not in unique:unique[key]={**row,'source_paths':[]}
        if row['path'] not in unique[key]['source_paths']:unique[key]['source_paths'].append(row['path'])
    rows=sorted(unique.values(),key=lambda r:(bool(r.get('candidate')),r.get('target_score',0),r.get('margin',0)),reverse=True)
    if a.limit:rows=rows[:a.limit]
    a.output.mkdir(parents=True,exist_ok=True)
    selected=[]; failures=[]
    for row in rows:
        for source in row['source_paths']:
            try:
                path=Path(source); st=path.stat()
                if getattr(st,'st_file_attributes',0)&(0x1000|0x40000|0x400000):raise ValueError('Nonresident placeholder')
                with Image.open(path) as image:
                    image.draft('RGB',(512,512)); thumb=ImageOps.exif_transpose(image).convert('RGB')
                    thumb.thumbnail((280,210))
                selected.append(({**row,'thumbnail_source':source},thumb));break
            except Exception as exc:failures.append({'path':source,'sha256':row['sha256'],'error':str(exc)})
    sheets=[]; mapping=[]
    for start in range(0,len(selected),24):
        batch=selected[start:start+24]; canvas=Image.new('RGB',(1200,6*260),'#eeeeee'); draw=ImageDraw.Draw(canvas)
        sheet_number=start//24+1
        for offset,(row,thumb) in enumerate(batch):
            number=start+offset+1; x=(offset%4)*300; y=(offset//4)*260
            canvas.paste(thumb,(x+(300-thumb.width)//2,y))
            draw.text((x+6,y+213),f"{number}: {row['category'][:31]}",fill='black')
            draw.text((x+6,y+231),str(row.get("contextual_evidence",{}).get("observation", "Legacy ranking candidate"))[:43],fill='black')
            mapping.append({'number':number,'sheet':sheet_number,**row})
        destination=a.output/f'candidates-{sheet_number:02d}.jpg';canvas.save(destination,quality=88);sheets.append(str(destination))
    (a.output/'mapping.json').write_text(json.dumps(mapping,indent=2),encoding='utf-8')
    (a.output/'errors.json').write_text(json.dumps(failures,indent=2),encoding='utf-8')
    print(json.dumps({'candidates':len(mapping),'sheets':sheets,'errors':len(failures)}))

if __name__=='__main__':main()
