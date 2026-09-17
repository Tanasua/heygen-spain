"""
Генерація обкладинки одним запитом до gpt-image-2.

Раніше було два кроки: gpt-image-1 малював фон, а заголовок накладався
через Pillow. Виходило плоско й аматорськи. Тепер один запит до
gpt-image-2 будує всю композицію разом із вбудованим заголовком.

ВАЖЛИВО — чому тут 9 варіантів, а не один промпт. Реальні обкладинки
українських новинних каналів (УНІАН тощо) не мають одного шаблону: у них
ротація помітно різних макетів — гігантське "ударне" слово на плашці,
червона стрічка під золотим написом, три яруси з лівим вирівнюванням,
тактична карта зі стрілками, блискавки навколо героя тощо. Один
усереднений промпт дає одноманітну стрічку однакових картинок, що вбиває
CTR. Тому варіант вибирається детерміновано за seed (id відео) — стрічка
виглядає різноманітно, але для конкретного відео результат відтворюваний.
"""

import base64
import hashlib
from io import BytesIO

import requests
from openai import OpenAI
from PIL import Image

CANVAS_SIZE = (1280, 720)  # стандарт YouTube 16:9

# Ходимо в Images API напряму, а не через SDK. Причина конкретна: gpt-image-2
# з параметром quality не проходить через openai==1.51.0, а підняти SDK до
# 2.x не можна — там Whisper (audio.transcriptions) починає віддавати 404 і
# весь дубляж падає (перевірено на реальному прогоні). REST-контракт від
# версії SDK не залежить, тож так стабільніше.
IMAGES_EDIT_URL = "https://api.openai.com/v1/images/edits"

# 1536x1024 (3:2) обрізався до 16:9 кропом 80px зверху/знизу і зрізав нижній
# рядок напису. "1568x896" — ratio 1.75 (майже 16:9=1.778), кроп ~7px.
GEN_SIZE = "1568x896"

# Спільна частина: персонажі з референсу, контрове світло, безпечні поля.
BASE_PROMPT = """\
Usa esta imagen como referencia y crea una miniatura de YouTube de noticias \
en el estilo visual de los canales informativos ucranianos.

PERSONAJES: mantén EXACTAMENTE a las mismas personas de la imagen de \
referencia — misma cara, misma persona reconocible. Recórtalas del fondo y \
compón un collage de noticias. NO inventes personas que no estén en la \
referencia.

ILUMINACIÓN: luz de contorno BLANCA e intensa detrás de las figuras \
principales — un halo de contraluz que recorta la silueta y la separa del \
fondo. Rim light blanco profesional y nítido, no un resplandor difuso.

TIPOGRAFÍA CON VOLUMEN REAL (esto es lo más importante del diseño): el \
titular NO debe ser texto plano con borde. Debe ser un rótulo tridimensional \
de verdad: letras con EXTRUSIÓN 3D visible y profunda (los laterales de cada \
letra se ven en perspectiva), bisel en los cantos, brillo especular en la \
cara superior de las letras, sombra proyectada sobre la escena. Como un \
letrero físico construido dentro del encuadre, con la misma luz que la \
escena. Tipografía sans-serif condensada muy pesada, MAYÚSCULAS.

TEXTO EXACTO del titular, respetando tildes y signos españoles \
(á é í ó ú ñ ¡ ¿) — escríbelo sin errores ortográficos:

"{headline}"

MARGEN DE SEGURIDAD OBLIGATORIO: ningún elemento del texto puede tocar los \
bordes. Deja libre al menos un 8% del alto arriba y abajo, y un 6% del ancho \
a izquierda y derecha. El titular completo debe caber holgadamente dentro de \
esa zona segura.

TONO: clickbait moderado y creíble, como un medio informativo serio, no \
caricaturesco.

Contexto del video (solo para ambientación, no agregues elementos ajenos a \
la referencia): {scene_hint}

Formato 16:9. Sin marcas de agua y sin logotipos de canales reales.

DISEÑO CONCRETO DE ESTA MINIATURA:
{variant}\
"""

# Дев'ять макетів, знятих із реальної стрічки новинного каналу.
# Кожен — окремий композиційний і типографічний прийом, а не варіація кольору.
VARIANTS = [
    # 1. Дуель: два антагоністи один проти одного, текст по центру внизу.
    """Composición de DUELO: dos figuras antagonistas enfrentadas, una a cada \
lado del encuadre, mirándose o mirando a cámara, con un escenario bélico \
oscuro entre ellas. Titular centrado en el tercio inferior, en DOS líneas: \
la primera en BLANCO, la segunda en AMARILLO intenso (#FFD400), ambas con \
extrusión 3D y borde negro grueso.""",

    # 2. Мега-слово: маленький верхній рядок, гігантське жовте слово на плашці.
    """Composición de PALABRA GIGANTE: la primera línea del titular en BLANCO \
y en tamaño pequeño, y la palabra final —la más impactante— en AMARILLO y \
ENORME, ocupando casi todo el ancho del encuadre, con extrusión 3D muy \
marcada. Detrás del texto, una franja oscura sólida que atraviesa la imagen \
para máxima legibilidad. Sujeto principal a un lado, evento dramático \
(fuego, humo, destrucción) al otro.""",

    # 3. Червона стрічка: золотий напис на банері.
    """Composición de BANDA ROJA: el titular va sobre una cinta o banner ROJO \
intenso que cruza la parte inferior en ligera diagonal, con las letras en \
DORADO/AMARILLO con relieve metálico y bisel brillante. Bordes de la cinta \
con sombra proyectada sobre la foto. Escena de fondo con las figuras de la \
referencia en primer plano.""",

    # 4. Три яруси зліва: багаторядковий текст із лівим вирівнюванням.
    """Composición de TRES NIVELES: el titular dividido en tres líneas \
apiladas y alineadas a la IZQUIERDA, alternando colores BLANCO / AMARILLO / \
BLANCO, sobre una franja oscura semitransparente vertical en el lado \
izquierdo. Las figuras de la referencia ocupan la mitad derecha del \
encuadre, en primer plano y con contraluz blanco.""",

    # 5. Червона стрілка-акцент.
    """Composición con FLECHA: una flecha curva ROJA grande y gruesa, con \
volumen 3D y sombra, que apunta hacia el elemento clave de la escena \
(la persona o el objeto del que trata la noticia). Titular en dos líneas, \
BLANCO y AMARILLO, en la esquina inferior izquierda. La flecha debe dirigir \
la mirada, no tapar las caras.""",

    # 6. Блискавки / енергетичні акценти.
    """Composición ELÉCTRICA: rayos y descargas eléctricas de color azul \
brillante y blanco alrededor de las figuras principales, como si la escena \
estuviera cargada de energía. Titular en dos líneas centradas abajo, BLANCO \
arriba y AMARILLO abajo, con extrusión 3D y un leve brillo eléctrico en los \
cantos de las letras.""",

    # 7. Портрет крупно + подія позаду.
    """Composición de REACCIÓN: primer plano grande de la cara de la persona \
de la referencia en un lado, con expresión intensa, y al otro lado una \
explosión o incendio a media distancia. Titular en dos líneas en la parte \
inferior, BLANCO y AMARILLO, con extrusión 3D pronunciada y sombra dura \
sobre el fondo.""",

    # 8. Розділений екран.
    """Composición de PANTALLA DIVIDIDA: el encuadre partido en dos mitades \
por una línea vertical luminosa (blanca o amarilla) con brillo. En cada \
mitad, una escena o una figura distinta de la referencia. El titular cruza \
la parte inferior sobre ambas mitades, en dos líneas BLANCO y AMARILLO con \
volumen 3D.""",

    # 9. Текст угорі.
    """Composición con TITULAR ARRIBA: el titular ocupa la franja SUPERIOR \
del encuadre (no la inferior), en dos líneas, BLANCO y AMARILLO, con \
extrusión 3D fuerte y sombra proyectada hacia abajo sobre la escena. Las \
figuras de la referencia ocupan la mitad inferior, grandes y con contraluz \
blanco, mirando hacia arriba o a cámara.""",
]


def _pick_variant(seed: str) -> int:
    """Детермінований вибір макета: те саме відео завжди дає той самий
    дизайн, але сусідні відео в стрічці виглядають по-різному."""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return digest[0] % len(VARIANTS)


def _to_16_9(img: Image.Image) -> Image.Image:
    """Дотягує до 16:9. Кроп бере переважно зверху — у більшості макетів
    заголовок унизу, тож краще жертвувати верхом фону, ніж низом тексту."""
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
                        scene_hint: str, output_path: str, variant_seed: str = "",
                        variant: int = None, quality: str = "high") -> str:
    """
    Один запит до gpt-image-2. Макет береться з ротації VARIANTS:
    variant — явний індекс (для тестів), інакше вибір за variant_seed
    (зазвичай video_id).

    quality="high" (~$0.165/зображення) можна знизити до "medium" (~$0.041).
    """
    index = variant if variant is not None else _pick_variant(variant_seed or headline)
    index %= len(VARIANTS)
    print(f"[THUMBNAIL] Макет #{index + 1} з {len(VARIANTS)}")

    prompt = BASE_PROMPT.format(
        headline=headline.upper(),
        scene_hint=scene_hint,
        variant=VARIANTS[index],
    )

    with open(reference_image_path, "rb") as f:
        resp = requests.post(
            IMAGES_EDIT_URL,
            headers={"Authorization": f"Bearer {client.api_key}"},
            data={
                "model": "gpt-image-2",
                "prompt": prompt,
                "size": GEN_SIZE,
                "quality": quality,
            },
            files={"image": ("reference.jpg", f, "image/jpeg")},
            timeout=300,
        )
    resp.raise_for_status()

    image_bytes = base64.b64decode(resp.json()["data"][0]["b64_json"])
    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    img = _to_16_9(img)
    img.save(output_path, "JPEG", quality=92)
    return output_path
