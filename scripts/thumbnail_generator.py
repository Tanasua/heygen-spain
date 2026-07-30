"""
Генерація обкладинки для іспанського відео.

Підхід (навмисно, а не "все одним промптом"):
1. OpenAI Images API (gpt-image-1) генерує ФОН/СЦЕНУ на основі оригінального
   thumbnail як референсу — без тексту в промпті.
2. Текст заголовка накладається окремо через Pillow — це надійніше, бо
   image-моделі часто спотворюють текст (особливо з іспанськими діакритиками:
   á, é, í, ó, ú, ñ, ¡, ¿).

Якщо хочете спробувати "все одним промптом" (як у вашому оригінальному
формулюванні) — розкоментуйте generate_thumbnail_single_prompt() і
використовуйте її замість generate_thumbnail_background(). Але майте на увазі
ризик кривого/нечитабельного тексту.
"""

import base64
import os
from io import BytesIO

from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont

CANVAS_SIZE = (1280, 720)  # стандарт YouTube 16:9


def generate_thumbnail_background(client: OpenAI, reference_image_path: str, scene_hint: str) -> Image.Image:
    """
    Генерує фонове зображення 16:9 на основі референсного кадру.
    scene_hint — короткий опис теми відео (наприклад, "ataque de misiles en la ciudad").
    """
    with open(reference_image_path, "rb") as f:
        result = client.images.edit(
            model="gpt-image-1",
            image=f,
            prompt=(
                f"Analiza esta imagen y crea una nueva escena de fondo estilo miniatura "
                f"clickbait de noticias de guerra, formato 16:9, dramática, alto contraste, "
                f"colores saturados (rojo, naranja, negro), sin ningún texto ni letras. "
                f"Tema: {scene_hint}."
            ),
            size="1536x1024",
        )

    image_b64 = result.data[0].b64_json
    image_bytes = base64.b64decode(image_b64)
    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    img = img.resize(CANVAS_SIZE)
    return img


def add_headline_text(img: Image.Image, headline: str, font_path: str = None) -> Image.Image:
    """
    Накладає клікбейт-заголовок у стилі зразків: жирний шрифт, жовтий/білий
    текст із чорною обводкою, у нижній третині кадру.
    """
    img = img.copy()
    draw = ImageDraw.Draw(img)

    font_size = 90
    if font_path and os.path.exists(font_path):
        font = ImageFont.truetype(font_path, font_size)
    else:
        # Fallback: якщо кастомний жирний шрифт не підключено, беремо стандартний.
        # РЕКОМЕНДАЦІЯ: покладіть .ttf жирного шрифту (напр. Montserrat-ExtraBold)
        # у папку assets/fonts/ і вкажіть шлях у font_path.
        font = ImageFont.load_default()

    max_width = CANVAS_SIZE[0] - 80
    lines = _wrap_text(draw, headline.upper(), font, max_width)

    line_height = font_size + 10
    total_height = line_height * len(lines)
    y = CANVAS_SIZE[1] - total_height - 40

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        text_width = bbox[2] - bbox[0]
        x = (CANVAS_SIZE[0] - text_width) / 2

        # Обводка (stroke) для читабельності на будь-якому фоні
        draw.text((x, y), line, font=font, fill="#FFD400",
                   stroke_width=6, stroke_fill="black")
        y += line_height

    return img


def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def generate_thumbnail(client: OpenAI, reference_image_path: str, headline: str,
                        scene_hint: str, output_path: str, font_path: str = None) -> str:
    bg = generate_thumbnail_background(client, reference_image_path, scene_hint)
    final = add_headline_text(bg, headline, font_path=font_path)
    final.save(output_path, "JPEG", quality=92)
    return output_path
