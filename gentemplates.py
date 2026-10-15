"""Generation templates for the Mini App (like Mira's): a style applied to the user's own photo, or to what they describe.

Each template's preview picture is drawn once by the bot itself (the free Flux route) and kept in tpl_previews/ next
to the database, so the cards show a real example. Until a preview exists the card shows its colours and emoji.
"""
import asyncio
import io
import json
import logging
import os
from pathlib import Path

LOG = logging.getLogger("secret_ai.templates")
FOLDER = Path(os.environ.get("DATABASE_PATH", "memory.db")).resolve().parent / "tpl_previews"

# key, kind, emoji, (ru title, en title), (ru subtitle, en subtitle), style added to the request, preview prompt, colours
TEMPLATES = [
    ("head3d", "photo", "🙂", ("3D голова", "3D head"), ("мультяшная 3D-версия", "a cartoon 3D version"),
     "turn the person into a stylized 3D animated movie character head, big expressive eyes, smooth skin, soft studio light, "
     "plain light grey background, keep their face recognizable",
     "3D animated movie character portrait of a smiling young man with brown wavy hair, pixar style, plain light grey "
     "background, soft studio light, head and shoulders", "#9ca3af", "#4b5563"),
    ("fighter", "photo", "⚡", ("Боец арены", "Arena fighter"), ("как герой файтинга", "a fighting-game hero"),
     "turn the person into an epic fighting video game character, ornate armor, lightning magic in the hands, dark arena, "
     "dramatic rim light, keep their face recognizable",
     "epic fighting video game character portrait, bearded man in blue ornate armor with lightning in his hand, "
     "dark background, dramatic rim light, ultra detailed", "#1e3a8a", "#0f172a"),
    ("clay", "photo", "🧱", ("Пластилин", "Clay"), ("пластилиновая версия", "a plasticine version"),
     "turn the person into a claymation plasticine figure with big eyes and a funny face, handmade texture, real street "
     "background, keep their face recognizable",
     "claymation plasticine figure of a funny man with big bulging eyes and a toothy grin, grey t-shirt, standing on a "
     "seaside promenade, photo, handmade texture", "#f59e0b", "#b45309"),
    ("waterline", "photo", "🌊", ("На границе воды", "At the waterline"), ("полу под водой", "half underwater"),
     "split-level over-under photo of the person half submerged in clear sea water, tropical fish below the waterline, "
     "sunlight caustics, keep their face recognizable",
     "split level over under water photo of a young woman with wet dark hair half submerged in clear turquoise sea, "
     "tropical fish below the waterline, sunlight caustics, photorealistic", "#0ea5e9", "#0f766e"),
    ("glass", "photo", "🥤", ("POV из стакана", "POV from a glass"), ("вид изнутри стакана", "a view from inside a glass"),
     "point of view from inside a milkshake glass looking up at the person drinking through a straw, wide angle fisheye, "
     "drops of milkshake and sprinkles, keep their face recognizable",
     "point of view from inside a milkshake glass looking up at a man drinking through a straw, wide angle fisheye lens, "
     "milkshake drips and sprinkles on the glass, photorealistic", "#fbbf24", "#a16207"),
    ("anime", "photo", "🌸", ("Аниме", "Anime"), ("как кадр из аниме", "like an anime frame"),
     "anime key visual of the person, Makoto Shinkai style, vibrant colors, detailed background, keep their features",
     "anime key visual portrait of a girl with long dark hair under cherry blossoms at sunset, Makoto Shinkai style, "
     "vibrant colors", "#a78bfa", "#db2777"),
    ("cyber", "photo", "🌃", ("Киберпанк", "Cyberpunk"), ("неон и ночной город", "neon and a night city"),
     "cyberpunk portrait of the person, neon lights, rainy night city, cinematic, ultra detailed, keep their face recognizable",
     "cyberpunk portrait of a young man in a reflective jacket, neon signs, rainy night city street, cinematic, ultra detailed",
     "#22d3ee", "#4338ca"),
    ("studio", "photo", "📸", ("Студийный портрет", "Studio portrait"), ("как фотосессия", "like a photo shoot"),
     "professional studio portrait photo of the person, 85mm lens, soft key light, dark grey backdrop, shallow depth of field",
     "professional studio portrait photo of a confident woman in a black turtleneck, 85mm lens, soft key light, dark grey "
     "backdrop, shallow depth of field", "#94a3b8", "#334155"),
    ("toy", "photo", "🧸", ("3D-игрушка", "3D toy"), ("коллекционная фигурка", "a collectible figurine"),
     "the person as a cute collectible 3D toy figurine in a toy box, glossy plastic, soft studio lighting, keep their features",
     "cute collectible 3D toy figurine of a smiling boy with a hoodie in a transparent toy box, glossy plastic, soft studio "
     "light", "#f472b6", "#be123c"),
    ("watercolor", "photo", "🎨", ("Акварель", "Watercolor"), ("нежный рисунок", "a soft painting"),
     "delicate watercolor painting of the person, soft pastel colors, paper texture, keep their features",
     "delicate watercolor portrait painting of a young woman with a sun hat, soft pastel colors, visible paper texture",
     "#38bdf8", "#6366f1"),
    ("cinema", "video", "🎬", ("Кино", "Cinema"), ("кинематографичный ролик", "a cinematic clip"),
     "cinematic shot, smooth camera movement, film look, dramatic lighting",
     "cinematic film still, a lone rider on a horse on a hill at golden hour, anamorphic lens, film grain, dramatic light",
     "#6366f1", "#9d174d"),
    ("animevideo", "video", "✨", ("Аниме-видео", "Anime video"), ("оживший аниме-кадр", "an anime frame comes alive"),
     "anime style animation, gentle wind, flowing hair, magical particles",
     "anime key visual of a girl on a rooftop at night, wind in her hair, glowing magical particles, city lights below",
     "#8b5cf6", "#0f766e"),
]


def public(lang):
    """The templates as the Mini App needs them (the preview prompts stay on the server)."""
    i = 0 if lang == "ru" else 1
    return [{"key": key, "kind": kind, "emoji": emoji, "title": titles[i], "sub": subs[i], "style": style,
             "c1": c1, "c2": c2, "preview": (FOLDER / f"{key}.jpg").exists()}
            for key, kind, emoji, titles, subs, style, _, c1, c2 in TEMPLATES]


def page_json():
    """Both languages for the page, embedded at load time: no extra request before the cards show."""
    return json.dumps({"ru": public("ru"), "en": public("en")}, ensure_ascii=False)


def preview(key):
    """The preview JPEG of a template, or None."""
    if not any(key == t[0] for t in TEMPLATES):
        return None
    path = FOLDER / f"{key}.jpg"
    return path.read_bytes() if path.exists() else None


def _shrink(image):
    """A 3:4 card picture, about 60 KB."""
    from PIL import Image
    picture = Image.open(io.BytesIO(image)).convert("RGB")
    width, height = picture.size
    if width / height > 0.75:  # too wide: crop the sides
        new = int(height * 0.75)
        picture = picture.crop(((width - new) // 2, 0, (width - new) // 2 + new, height))
    picture.thumbnail((540, 720))
    out = io.BytesIO()
    picture.save(out, "JPEG", quality=82, optimize=True)
    return out.getvalue()


async def draw_previews(data, free_image):
    """Draw the missing previews one by one (the free route allows about one picture per 15 seconds)."""
    if os.environ.get("TEMPLATE_PREVIEWS", "1").strip() == "0":
        return
    FOLDER.mkdir(parents=True, exist_ok=True)
    for key, *_, prompt, _c1, _c2 in TEMPLATES:
        path = FOLDER / f"{key}.jpg"
        if path.exists():
            continue
        try:
            image = await free_image(data["http"], prompt, "portrait", data.get("cloudflare"))
            path.write_bytes(await asyncio.to_thread(_shrink, image))
            LOG.info("Template preview ready: %s", key)
        except Exception as exc:  # the next start tries again
            LOG.warning("Template preview %s failed: %s %s", key, type(exc).__name__, str(exc)[:150])
        await asyncio.sleep(16)
