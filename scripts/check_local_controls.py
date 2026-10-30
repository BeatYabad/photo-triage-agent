"""Opt-in real model check on in-memory controls, with no gallery access."""
import json
from PIL import Image, ImageDraw
from photo_triage.vision import VisionProvider, junk_result


def run():
    provider=VisionProvider('http://127.0.0.1:18765/v1/chat/completions',model='Qwen3-VL-4B-Instruct-Q8_0')
    rows=[]
    for name,text in [
        ('ordinary_opening_hours','WELCOME\nOPENING HOURS\nMON-FRI 9 AM - 5 PM\nSATURDAY 10 AM - 2 PM'),
        ('personal_school_record','SCHOOL REPORT\nStudent: Example Student\nMathematics: A\nScience: A\nTeacher comment: Great progress!'),
        ('reference_instructions','HOW TO WATER YOUR PLANT\nWater when the top inch of soil is dry.\nKeep near a bright window.\nDo not leave standing in water.'),
        ('ordinary_flat_image','')]:
        image=Image.new('RGB',(800,600),'#8b9a85')
        draw=ImageDraw.Draw(image)
        if text:
            draw.rectangle((35,70,765,530),fill='#f1ebd3',outline='#47412e',width=10)
            draw.multiline_text((60,115),text,fill='black',font_size=25,spacing=20)
        result=junk_result(provider.inspect(image),list(image.size))
        rows.append({'control':name,**result})
    print(json.dumps(rows,indent=2))
    return 1 if any(row['candidate'] or row['status']!='analyzed' for row in rows) else 0


if __name__=='__main__':raise SystemExit(run())
