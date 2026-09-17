"""
Генерація іспанського title/description/тегів для завантаженого відео,
у стилі каналу @AHORAMISMO (клікбейт, war-news формат).
"""

import json
from openai import OpenAI

SYSTEM_PROMPT = """Eres un editor de un canal de YouTube de noticias sobre la guerra \
en Ucrania, dirigido a audiencia hispanohablante. Tu estilo es clickbait pero \
basado en hechos: títulos en mayúsculas parciales, con signos de exclamación, \
palabras impactantes (URGENTE, ÚLTIMA HORA, IMPACTANTE), similar a canales \
como "AHORA MISMO". Nunca inventes hechos que no estén en el título/descripción \
original — solo adapta el tono y el idioma."""

USER_PROMPT_TEMPLATE = """Título original (ruso/ucraniano): {original_title}

Genera:
1. Un título en español, máximo 100 caracteres, estilo clickbait de noticias de guerra.
2. Una descripción en español de 3-5 líneas, con 3-5 hashtags relevantes al final.
3. Una frase corta (5-8 palabras) en español para usar como texto de portada/miniatura.
4. Una lista de 8-12 etiquetas (tags) en español para el campo "tags" de YouTube —
   palabras o frases cortas SIN el símbolo #, relevantes al tema del video.

Responde ÚNICAMENTE en formato JSON, sin texto adicional, con esta estructura exacta:
{{"title": "...", "description": "...", "thumbnail_headline": "...", "tags": ["...", "..."]}}"""


def generate_spanish_metadata(client: OpenAI, original_title: str) -> dict:
    response = client.chat.completions.create(
        model="gpt-4.1",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT_TEMPLATE.format(original_title=original_title)},
        ],
        response_format={"type": "json_object"},
        temperature=0.8,
    )

    content = response.choices[0].message.content
    data = json.loads(content)

    required_keys = {"title", "description", "thumbnail_headline", "tags"}
    if not required_keys.issubset(data.keys()):
        raise ValueError(f"Відповідь OpenAI не містить очікуваних полів: {data}")

    return data
