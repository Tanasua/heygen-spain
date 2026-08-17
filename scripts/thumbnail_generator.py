"""
Генерація обкладинки одним запитом до gpt-image-2.

Раніше (gpt-image-1) фон і текст робились окремо: image-модель малювала
сцену без тексту, а заголовок накладав Pillow — бо gpt-image-1 часто
спотворював текст (особливо іспанські діакритики: á, é, í, ó, ú, ñ, ¡, ¿).

gpt-image-2 (реліз OpenAI, квітень 2026) істотно покращив рендер тексту
всередині зображення, тож тепер один запит робить обидві речі:
1. Бере оригінальний YouTube thumbnail як референс і трохи змінює ракурс/
   кадрування — ті самі люди й об'єкти, що й в оригіналі, без вигадування
   нових.
2. Одразу вбудовує клікбейт-заголовок (той самий текст, що згенерував GPT
   у metadata_generator.py) у стилі референсних обкладинок
   @AHORAMISMO-b5e / @GlavredTV: жирний напис, неон/обводка, високий
   контраст.

Якщо якість напису виявиться нестабільною на практиці — це перше, що
варто перевірити на кількох реальних відео перед тим, як покладатись на
це в проді.
"""

import base64
from io import BytesIO

from openai import OpenAI
from PIL import Image

CANVAS_SIZE = (1280, 720)  # стандарт YouTube 16:9

# 1536x1024 (3:2, офіційний приклад з документації) обрізався до 16:9
# центральним кропом 80px зверху/знизу — і зрізав нижній рядок напису,
# який модель ставить у нижню третину (реальний баг, побачений на
# першому опублікованому відео). "1568x896" — теж кратне 16, ratio 1.75
# (майже 16:9=1.778) — залишає кроп ~7px замість 80px.
GEN_SIZE = "1568x896"

STYLE_PROMPT = """\
Analiza esta imagen de referencia (miniatura original de un video de \
noticias) y genera una nueva miniatura de YouTube en el mismo estilo que \
usan los canales de noticias de guerra en español tipo "AHORA MISMO": \
fondo dramático de alto contraste, colores saturados (rojo, naranja, \
negro), a veces con efecto de fuego, explosión o resplandor neón sobre \
el sujeto principal.

Mantén EXACTAMENTE las mismas personas y los mismos objetos de la imagen \
original — misma cara, mismo objeto, misma escena reconocible. Solo \
cambia ligeramente el ángulo de cámara, el encuadre o el zoom, como si \
fuera una segunda toma de la misma escena. NO inventes personas ni \
objetos nuevos que no estén en la imagen original.

Incorpora el titular directamente DENTRO de la imagen (no lo describas, \
renderízalo como texto real y legible): mayúsculas, tipografía muy \
gruesa tipo impacto, color amarillo (#FFD400) o blanco con borde negro \
grueso o efecto de brillo neón, ubicado donde mejor encaje con la \
composición (normalmente tercio inferior). El titular exacto es:

"{headline}"

IMPORTANTE — margen de seguridad: deja al menos un 8% del alto de la \
imagen totalmente libre en el borde superior Y en el borde inferior (sin \
texto, sin partes de caras, sin objetos clave pegados al borde) — esa \
franja se recorta después para ajustar a 16:9, y cualquier elemento \
pegado al borde quedará cortado.

Contexto/tema del video (solo para ambientación, no agregues elementos \
que no estén ya en la imagen): {scene_hint}

Formato 16:9, estilo miniatura clickbait de noticias, sin marcas de agua, \
sin texto adicional aparte del titular indicado.\
"""


def _to_16_9(img: Image.Image) -> Image.Image:
    """Дотягує до 16:9. Вертикальний кроп бере переважно зверху — заголовок
    завжди в нижній третині, тож краще жертвувати верхом фону, ніж низом тексту."""
    target_ratio = CANVAS_SIZE[0] / CANVAS_SIZE[1]
    w, h = img.size
    current_ratio = w / h
    if current_ratio > target_ratio:
        new_w = round(h * target_ratio)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    elif current_ratio < target_ratio:
        new_h = round(w / target_ratio)
        removed = h - new_h
        top = round(removed * 0.85)
        img = img.crop((0, top, w, top + new_h))
    return img.resize(CANVAS_SIZE)


def generate_thumbnail(client: OpenAI, reference_image_path: str, headline: str,
                        scene_hint: str, output_path: str, quality: str = "high") -> str:
    """
    Один запит до gpt-image-2: змінює ракурс референсного кадру і вбудовує
    заголовок. quality="high" ($≈0.165/зображення) — можна знизити до
    "medium" ($≈0.041), якщо якість не критична, а вартість важливіша.
    """
    prompt = STYLE_PROMPT.format(headline=headline.upper(), scene_hint=scene_hint)

    with open(reference_image_path, "rb") as f:
        result = client.images.edit(
            model="gpt-image-2",
            image=f,
            prompt=prompt,
            size=GEN_SIZE,
            quality=quality,
        )

    image_bytes = base64.b64decode(result.data[0].b64_json)
    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    img = _to_16_9(img)
    img.save(output_path, "JPEG", quality=92)
    return output_path
