"""
Генерація обкладинки одним запитом до gpt-image-2.

Раніше було два кроки: gpt-image-1 малював фон, а заголовок накладався
через Pillow. Виходило плоско й аматорськи — рівний жовтий текст із
обводкою, наліплений поверх картинки.

Тепер один запит до gpt-image-2 робить усе разом: бере оригінальний
YouTube-thumbnail як референс (ті самі люди/об'єкти), домальовує сцену і
одразу вбудовує об'ємний заголовок як частину композиції.

Стиль орієнтований на українські новинні YouTube-канали (УНІАН тощо) —
з реальних обкладинок зчитано: двоколірний напис (білий рядок + жовтий
"ударний" рядок), важкий вузький гротеск ALL CAPS, товста чорна обводка з
тінню, вирізані фігури персонажів на драматичному фоні, білий контровий
світловий контур, який відділяє фігури від фону.

Логотипи/брендинг реальних каналів навмисно НЕ відтворюються.
"""

import base64
from io import BytesIO

from openai import OpenAI
from PIL import Image

CANVAS_SIZE = (1280, 720)  # стандарт YouTube 16:9

# 1536x1024 (3:2) обрізався до 16:9 кропом 80px зверху/знизу і зрізав нижній
# рядок напису. "1568x896" — ratio 1.75 (майже 16:9=1.778), кроп ~7px.
GEN_SIZE = "1568x896"

STYLE_PROMPT = """\
Usa esta imagen como referencia y crea una miniatura de YouTube de noticias \
en el estilo visual de los canales informativos ucranianos (tipo UNIAN).

PERSONAJES: mantén EXACTAMENTE a las mismas personas y objetos de la imagen \
de referencia — misma cara, misma persona reconocible. Recórtalos del fondo \
y compón la escena como un collage de noticias: la figura principal grande y \
nítida en un lado del encuadre. NO inventes personas que no estén en la \
referencia.

ILUMINACIÓN (importante): aplica una luz de contorno BLANCA e intensa detrás \
de las figuras principales — un halo de contraluz blanco que recorta la \
silueta y la separa claramente del fondo. Efecto de rim light blanco \
profesional, no un resplandor difuso.

FONDO: escena dramática relacionada con el tema — humo, fuego, explosión \
lejana, cielo oscuro, o escenario urbano/militar. Alto contraste, colores \
saturados pero no chillones. El fondo debe estar ligeramente desenfocado \
para que las figuras destaquen.

TITULAR — renderízalo como texto real, legible, integrado en la composición \
(NO lo describas): en MAYÚSCULAS, tipografía sans-serif condensada muy \
pesada (tipo Druk/Impact), con VOLUMEN: letras con extrusión 3D sutil, borde \
negro grueso y sombra proyectada, como rótulo diseñado profesionalmente. \
Divide el titular en 2 líneas: la primera línea en BLANCO y la segunda \
línea (la más impactante) en AMARILLO intenso (#FFD400). Colócalo en el \
tercio inferior o centrado abajo, ocupando buena parte del ancho.

El titular exacto, respetando tildes y signos españoles (á é í ó ú ñ ¡ ¿):

"{headline}"

TONO: clickbait moderado y profesional — llamativo pero creíble, como un \
medio informativo serio, NO exagerado ni caricaturesco.

MARGEN DE SEGURIDAD: deja al menos un 8% del alto totalmente libre arriba Y \
abajo (sin texto ni partes de caras pegadas al borde) — esa franja se recorta \
después para ajustar a 16:9.

Contexto del video (solo para ambientación, no agregues elementos nuevos): \
{scene_hint}

Formato 16:9. Sin marcas de agua, sin logotipos de canales reales, sin texto \
adicional aparte del titular indicado.\
"""


def _to_16_9(img: Image.Image) -> Image.Image:
    """Дотягує до 16:9. Вертикальний кроп бере переважно зверху — заголовок
    у нижній третині, тож краще жертвувати верхом фону, ніж низом тексту."""
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
    Один запит до gpt-image-2: перекомпоновує референсний кадр у новинний
    колаж із контровим світлом і вбудованим об'ємним заголовком.
    quality="high" (~$0.165/зображення) можна знизити до "medium" (~$0.041),
    якщо вартість важливіша за якість.
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
