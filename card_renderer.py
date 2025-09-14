from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

W, H = 1080, 1920
BG = (245,241,236)
FG = (35,35,35)
ACCENT = (120,79,44)

def _font(size):
    try:
        ttf = Path(__file__).with_name("DejaVuSans.ttf")
        if ttf.exists():
            return ImageFont.truetype(str(ttf), size=size)
        return ImageFont.truetype("DejaVuSans.ttf", size=size)
    except Exception:
        return ImageFont.load_default()

def render_recipe_card(name, ingredients, method, out_path):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    title = _font(70); body = _font(38); small = _font(30)
    d.rectangle([0,0,W,200], fill=ACCENT)
    d.text((60,60), name, fill=(255,255,255), font=title)

    y = 260
    d.text((60,y), "Ingredients", fill=FG, font=body); y += 60
    for line in ingredients:
        d.text((80,y), f"• {line}", fill=FG, font=small); y += 46

    y += 30
    d.text((60,y), "Method", fill=FG, font=body); y += 60
    for i,line in enumerate(method,1):
        d.text((80,y), f"{i}. {line}", fill=FG, font=small); y += 46

    d.text((60, H-100), "Enjoy responsibly.", fill=(100,100,100), font=small)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, quality=95)
    return out_path
