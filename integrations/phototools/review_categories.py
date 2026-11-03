"""Stable, readable review folders selected from recorded script findings."""
from pathlib import Path

FOLDERS={
    'unreadable_original':'Unreadable',
    'presentation_slide':'Presentations',
    'web_graphic':'Internet graphics',
    'meme_or_cartoon':'Memes and cartoons','receipt':'Receipts','screenshot':'Screenshots',
    'store_price_comparison':'Shopping prices','restaurant_menu':'Menus',
    'product_packaging_label':'Product labels','event_notice_or_pass':'Tickets and notices',
    'computer_screen_or_slides':'Computer screens and slides','repair_reference':'Repair references',
    'uncertain-text-reference':'Text references','tiktok':'TikTok',
    'possible_blur_or_low_detail':'Blurry or low detail','nearly_all_dark':'Very dark',
    'nearly_all_bright':'Very bright','low_resolution':'Low resolution',
    'decoder_rejected_image':'Unreadable image','duplicate':'DUPES','live-photo':'Extra Live Photo stills',
    'live-photo-motion':'Extra Live Photo motion files',
    'unmatched-live-photo':'Live Photos',
    'import-decision':'Import decisions',
    'unknown':'Other review reasons'}


def create(root):
    # Category folders are created by the verified mover when placing a file.
    Path(root).mkdir(parents=True,exist_ok=True)


def reason(group,selected):
    if selected=='import_decision':return 'import-decision'
    if selected=='live_photo':return 'unmatched-live-photo'
    if selected=='tiktok':return 'tiktok'
    findings=[r.get('checks',{}).get(selected,{}) for r in group]
    findings=[f for f in findings if f.get('candidate')]
    if selected=='quality':
        reasons={reason for f in findings for reason in f.get('reasons',[])}
        return next((key for key in ('nearly_all_dark','nearly_all_bright','possible_blur_or_low_detail','low_resolution','decoder_rejected_image') if key in reasons),'unknown')
    return next((f['category'] for f in findings if f.get('category') in FOLDERS),'unknown')


def destination(config,group,selected):
    key=reason(group,selected)
    return Path(config['review_folder'])/FOLDERS[key],key
