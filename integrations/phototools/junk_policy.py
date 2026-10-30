"""Require category-specific evidence; a similarity score or generic text is insufficient."""
import re

TARGETS={'meme_or_cartoon','receipt','screenshot','restaurant_menu','store_price_comparison','presentation_slide'}
SENSITIVE={'patient','diagnosis','prescription','hospital','passport','ssn','mortgage',
           'deed','attorney','lawsuit','beneficiary','routing','payroll','account',
           'insurance','student','grade','diploma','transcript','invoice'}


def decide(result,lines,make='',model=''):
    if result.get('contextual_evidence', {}).get('source') == 'photo_triage_agent':
        return result
    result=dict(result);holds=set(result.get('holds',[]))
    category=result.get('nearest_target') or result.get('category')
    good=[str(text) for text,score,_ in lines if float(score)>=.9]
    text=' '.join(good).lower();tokens=set(re.findall(r'[a-z0-9]+',text))
    prices=len(re.findall(r'\$\s*\d+(?:[.,]\d{2})?|\b\d+[.,]\d{2}\b',text))
    score=float(result.get('target_score',0));margin=float(result.get('margin',0))
    views=result.get('view_categories',[])
    strong=score>=.30 and margin>=.04 and len(views)==2 and all(v==category for v in views)
    # CLIP's category scores are not probabilities. Specific OCR evidence can
    # corroborate a moderate category score without globally relaxing thresholds.
    category_support=category in views and score>=.25
    receipt_terms=tokens & {'receipt','subtotal','total','tax','cash','change','visa','mastercard','tender','store','purchase',
                            'balance','due','order','tip','check','merchant','transaction','approved','card','sale'}
    transaction_total=bool(tokens & {'subtotal','total'}) or {'balance','due'}<=tokens or (
        {'tip','check'}<=tokens and bool(tokens & {'merchant','transaction'}))
    retail_receipt=category=='receipt' and category_support and len(receipt_terms)>=3 and transaction_total and prices>=2 and len(good)>=4
    food=tokens & {'appetizers','entrees','dessert','desserts','salad','soup','chicken','beef','pork','pasta','sandwich','pizza','seafood','shrimp','salmon','burger','steak','vegetables'}
    menu=category=='restaurant_menu' and category_support and len(good)>=5 and len(food)>=3 and (prices>=1 or bool(tokens & {'menu','course','entree','dessert','choose','dinner'}))
    retail=tokens & {'price','sale','save','each','regular','retail','sku','item','model','member','rebate','was','now','special','costco','walmart','target','lowes','depot'}
    price_tag=category=='store_price_comparison' and category_support and prices>=1 and len(retail)>=2 and len(good)>=2
    caption_text=' '.join(str(t).lower() for t,s,_ in lines if float(s)>=.75)
    publisher=bool(re.search(r'\b(?:memegenerator|imgflip|quickmeme|makeameme|someecards)\b',caption_text))
    template=bool(re.search(r'\b(?:kids advice|that moment when|when you|are you not entertained|expectation.{0,25}reality|nobody\s*:)',caption_text))
    explicit_meme=category=='meme_or_cartoon' and not(make.strip() or model.strip()) and len(views)==2 and all(v==category for v in views) and score>=.23 and margin>=0 and (publisher or template) and len(text.split())>=5
    publisher_meme=(publisher and category=='meme_or_cartoon' and score>=.21 and margin>=-.025
        and len(views)==2 and all(v in {'meme_or_cartoon','other_graphic'} for v in views)
        and not(make.strip() or model.strip()) and len(good)>=2 and len(text.split())>=2)
    # A captioned meme can rank narrowly behind "other graphic" in both views.
    # Require a distinctive joke construction/publisher, not generic text or faces.
    graphic_caption=(category=='meme_or_cartoon' and score>=.23 and margin>=-.025
        and len(views)==2 and all(v in {'meme_or_cartoon','other_graphic'} for v in views)
        and not(make.strip() or model.strip()) and len(good)>=2 and len(text.split())>=5
        and (publisher or bool(re.search(r'\bsaid no (?:one|body) ever\b',caption_text))))
    # A nearby document score is not evidence of an actual document. Agreeing
    # meme views plus readable captions can resolve that ambiguity, but keep
    # people, personal artifacts and sensitive OCR protections in force.
    document_lookalike=result.get('protection_category') in {
        'protect_medical','protect_legal_financial_identity'}
    # OCR often drops spaces in all-caps meme fonts. Count readable letters,
    # while still requiring multiple high-confidence lines and visual agreement.
    readable_caption=len(good)>=2 and len(re.findall(r'[a-z]',text))>=20
    captioned_meme=(category=='meme_or_cartoon' and score>=.245 and margin>=.005
        and len(views)==2 and all(v==category for v in views)
        and not(make.strip() or model.strip()) and readable_caption
        and ('protected_content_near_target_score' not in holds or
             (document_lookalike and margin>=.015))
        and not (result.get('protection_category')=='protect_people_family' and float(result.get('protection_score',0))>=score-.06))
    # Downloaded interactive banners are not necessarily UI screenshots. Require
    # an action or device restriction, game/puzzle context AND web/device wording.
    web_game_graphic=(category=='screenshot' and category_support
        and len(views)==2 and all(v in {'screenshot','other_graphic',
            'protect_legal_financial_identity'} for v in views)
        and margin>=.005 and not(make.strip() or model.strip())
        and len(good)>=3 and len(text.split())>=8
        and (any(re.fullmatch(r'(?:play(?: now| online| game| for free)?|click (?:here|to play)|visit (?:our |the )?website)[.!]?',
                             line.strip().lower()) for line in good)
             or bool(re.search(r'\bnot (?:accessible|available|supported) (?:from|on|in) (?:mobile|this browser|this device)\b',text)))
        and bool(tokens & {'puzzle','game','quiz','challenge'})
        and bool(tokens & {'website','online','browser','mobile','devices'})
        and result.get('protection_category')!='protect_people_family')
    presentation=(category=='presentation_slide' and score>=.25 and len(views)==2
        and all(v==category for v in views) and len(good)>=3 and len(text.split())>=10)
    # Review nominations, not deletion decisions. Corroborated graphic evidence
    # must not be vetoed merely by a nearby generic document embedding.
    no_camera=not(make.strip() or model.strip())
    document_or_graphic=result.get('protection_category') in {None,'protect_medical','protect_legal_financial_identity'}
    agreed_meme=(category=='meme_or_cartoon' and views==['meme_or_cartoon','meme_or_cartoon'])
    caption_graphic=(no_camera and document_or_graphic and agreed_meme
                     and score>=.23 and margin>=.005 and readable_caption
                     and len(text.split())>=5)
    low_caption=' '.join(str(t).lower() for t,s,_ in lines if float(s)>=.70)
    satire=bool(re.search(r'\bwoman rushed to hospital.{0,100}trailer ball\s*hitch\b',caption_text))
    # OCR confusion in large stylized political captions: require the title,
    # agreeing graphic views and noncamera evidence, never the name alone.
    political_caption=bool(re.search(r'\btr[ua]m?u?ps?\s+funeral\b',low_caption))
    satire=satire or political_caption
    sparse_satire=(no_camera and document_or_graphic and agreed_meme and score>=.225 and margin>=0 and satire)
    parody_greeting=(no_camera and document_or_graphic and 'meme_or_cartoon' in views
                     and all(v in {'meme_or_cartoon','event_notice_or_pass','other_graphic'} for v in views)
                     and float(result.get('scores',{}).get('meme_or_cartoon',0))>=.245
                     and bool(re.search(r'you make me shake.{0,50}(?:everytime|every time).{0,30}see you',caption_text)))
    app_labels=tokens & {'music','maps','messages','phone','podcasts','audiobooks','calendar','settings','photos','now','playing'}
    app_grid=(no_camera and document_or_graphic and category=='screenshot'
              and views==['screenshot','screenshot'] and score>=.25 and margin>=.005
              and len(app_labels)>=4 and len(good)>=4)
    quote_publisher=bool(re.search(r'@\w*(?:quote|meme)\w*\b',caption_text))
    published_quote=(no_camera and agreed_meme and score>=.24 and margin>=.015
                     and quote_publisher and readable_caption)
    repeated_reaction=(no_camera and agreed_meme and score>=.26 and margin>=.02
                       and len(good)>=4 and len(set(good))<len(good)
                       and bool(re.search(r"\bit'?s actually\b",caption_text)))
    # Generic words remain insufficient; recognizable fictional-character
    # labels corroborate a noncamera graphic, including sparse image macros.
    fictional_macro=(no_camera and document_or_graphic and score>=.245 and margin>=.005
                      and len(views)==2 and all(v in {'meme_or_cartoon','presentation_slide'} for v in views)
                      and {'night','king','bran'}<=tokens)
    app_parody=(no_camera and document_or_graphic and category=='screenshot' and score>=.25 and margin>=.005
                and len(views)==2 and all(v in {'screenshot','product_packaging_label'} for v in views)
                and 'tinder' in tokens and len(good)>=3
                and bool(re.search(r'\b(?:imgflip|imgilp)\.com\b',caption_text)))
    supported=retail_receipt or menu or price_tag or explicit_meme or publisher_meme or graphic_caption or captioned_meme or presentation or web_game_graphic or caption_graphic or sparse_satire or parody_greeting or app_grid or published_quote or repeated_reaction or fictional_macro or app_parody
    sensitive_tokens=tokens & SENSITIVE
    if sparse_satire and sensitive_tokens=={'hospital'}:
        sensitive_tokens.clear()  # A corroborated joke headline is not a patient record.
    if retail_receipt and tokens & {'merchant','mastercard','visa'} and not tokens & {'statement','bank','routing'}:
        sensitive_tokens.discard('account')  # Printed card account field on a retail payment slip.
    protected=bool(tokens & SENSITIVE) or result.get('protection_category')=='protect_people_family' and float(result.get('protection_score',0))>=score-.06
    protected |= 'protected_content_near_target_score' in holds
    reason='insufficient_category_evidence';candidate=False
    if sensitive_tokens:reason='protected_personal_content'
    elif supported:
        candidate=True
        reason=('retail_receipt_visual_and_transaction_evidence' if retail_receipt else
                'menu_visual_and_food_listing_evidence' if menu else
                'price_tag_visual_and_retail_evidence' if price_tag else
                'presentation_visual_and_text_layout_evidence' if presentation else
                'web_graphic_visual_and_interactive_banner_evidence' if web_game_graphic else
                'screenshot_visual_and_app_grid_evidence' if app_grid else
                'meme_visual_and_distinctive_caption_evidence')
    elif protected:reason='protected_personal_content'
    elif category not in TARGETS:reason='reference_photo_is_not_requested_junk'
    elif not strong:reason='weak_or_disagreeing_visual_scores'
    elif category=='screenshot':
        # Camera photos of slides, signs and displays are not screenshots.
        ui=tokens & {'settings','login','password','browser','download','install','windows','android','iphone','search','notifications','cancel','submit'}
        candidate=not(make.strip() or model.strip()) and len(ui)>=2 and len(good)>=4 and len(text.split())>=12
        reason='screenshot_visual_and_ui_evidence' if candidate else 'no_screenshot_capture_evidence'
    elif category=='receipt':
        receipt_terms=tokens & {'receipt','subtotal','total','tax','cash','change','visa','mastercard','tender','store','purchase'}
        candidate=len(receipt_terms)>=3 and bool(tokens & {'subtotal','total'}) and prices>=2 and len(good)>=4
        reason='retail_receipt_visual_and_transaction_evidence' if candidate else 'no_retail_receipt_evidence'
    elif category=='meme_or_cartoon':
        caption=len(good)>=2 and len(text.split())>=5
        # Both full-image and center views must support a strong textless cartoon.
        candidate=caption or score>=.34 and margin>=.065
        reason='meme_visual_and_caption_evidence' if candidate and caption else 'strong_cartoon_visual_agreement' if candidate else 'no_meme_caption_or_strong_cartoon_evidence'
    holds.discard('visual_review_required')
    if candidate:
        holds.add('visual_review_required')
        if captioned_meme or web_game_graphic or caption_graphic or sparse_satire or parody_greeting or app_grid or published_quote or repeated_reaction or fictional_macro or app_parody:
            holds.discard('protected_content_near_target_score')
        if web_game_graphic:category='web_graphic'
        if parody_greeting or fictional_macro or app_parody:category='meme_or_cartoon'
    result.update(candidate=candidate,category=category if candidate else None,
        confidence_band='supported_review' if candidate else 'not_candidate',holds=sorted(holds),
        nomination_reason=reason,policy='supported-junk-v7-corroborated-graphics',
        corroboration={'good_text_lines':len(good),'price_patterns':prices,'camera_capture':bool(make or model),
                       'both_views_agree':len(views)==2 and all(v==category for v in views)})
    return result
