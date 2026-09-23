"""
DIY-дубляж без ліп-синку: Whisper (транскрипція) -> GPT (переклад) ->
Inworld TTS (voice cloning + синтез) -> ffmpeg (тайм-стрейч і склейка доріжки) ->
заміна аудіодоріжки в оригінальному відео.

Навіщо: HeyGen precision mode (ліп-синк) коштує ~$4/хв через API. Якщо
ліп-синк не потрібен, цей пайплан на кілька порядків дешевший (Whisper +
GPT-переклад тексту + Inworld TTS), бо кожен компонент тарифікується окремо
і без націнки турнкі-сервісу.

Обмеження, які варто знати:
- Немає ліп-синку — губи не збігаються з новим звуком.
- Inworld API (endpoints /voices/v1/voices:clone і /tts/v1/voice, Basic-auth
  заголовок "Authorization: Basic <API_KEY>") звірено з офіційними прикладами
  на момент написання, але langCode для клонування голосу в документації
  трапляється у двох форматах ("EN_US" і "ru"/"es"). Тут використовується
  формат "ru"/"uk"/"es", підтверджений для Realtime TTS-2 — якщо Inworld
  поверне 400 з описом іншого формату, виправте LANG_CODE_MAP і
  DEFAULT_TARGET_LANG_CODE нижче.
- Українську мову Inworld офіційно підтверджує лише як "ймовірно
  experimental" (не в списку GA-мов) — якість клонування голосу з
  українського джерела не гарантована.
- Озвучення йде не по одному сегменту Whisper, а кусками ~15 с
  (_group_segments), і темп тайм-стрейчу обмежений клемпом
  MAX_SLOWDOWN..MAX_SPEEDUP. Через це синхронність із картинкою
  приблизна (±частка секунди) — свідомий розмін заради того, щоб мова не
  тараторила і не жувалась. Ліп-синку тут усе одно немає.
"""

import base64
import os
import struct
import subprocess
import wave

SAMPLE_RATE = 24000
INWORLD_BASE_URL = "https://api.inworld.ai"
# ВАЖЛИВО: inworld-tts-1-max/1.5 НЕ cross-lingual — переносить акцент
# голосового зразка на цільову мову (звідси сильний акцент у першому
# тесті). inworld-tts-2 за замовчуванням намагається говорити цільовою
# мовою нативно, без акценту оригіналу. Мінус: tts-2 зараз research
# preview (не GA) — менш стабільний статус, ніж 1.5.
DEFAULT_MODEL_ID = "inworld-tts-2"

# Наразі не використовується в клонуванні (див. translate_video — зараз
# клонуємо з langCode цільової мови, не мови оригіналу, як експеримент
# проти акценту). Лишено для швидкого відкату, якщо експеримент не зайде.
# Whisper повертає повну назву мови ("russian", "ukrainian", ...).
# Inworld TTS-2 GA-мови (підтверджено офіційно): en, zh, ja, ko, ru, it,
# es, pt, fr, de, pl, nl, hi, he, ar. Українська — не в GA-списку.
LANG_CODE_MAP = {
    "russian": "ru",
    "ukrainian": "uk",
    "english": "en",
}
DEFAULT_SOURCE_LANG_CODE = "ru"
TARGET_LANG_CODE_MAP = {"es": "es"}

VOICE_REF_MIN_SECONDS = 6.0
VOICE_REF_MAX_SECONDS = 14.0

import requests
from openai import OpenAI


class DubError(RuntimeError):
    pass


def _run_ffmpeg(args: list[str], error_prefix: str) -> None:
    cmd = ["ffmpeg", "-y"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise DubError(f"{error_prefix}:\n{result.stderr[-2000:]}")


def _ffprobe_duration(path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise DubError(f"ffprobe помилка для {path}:\n{result.stderr[-1000:]}")
    return float(result.stdout.strip())


# Whisper API приймає файли максимум 25 МБ (26214400 байт). Сирий PCM WAV
# на 24кГц/mono важить ~5.5 МБ/хв — 30-хвилинне відео (наш ліміт для
# inworld) уже вивалюється за межі. mp3 64kbps ~0.48 МБ/хв — з великим
# запасом навіть на максимальну довжину.
WHISPER_SIZE_LIMIT_BYTES = 25 * 1024 * 1024


def _extract_audio_for_whisper(video_path: str, out_path: str, bitrate_kbps: int = 64) -> str:
    """Стиснуте аудіо (mp3) для відправки в Whisper — щоб не впертись у 25 МБ ліміт."""
    _run_ffmpeg(
        ["-i", video_path, "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
         "-c:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k", out_path],
        "ffmpeg помилка витягання аудіо",
    )
    size = os.path.getsize(out_path)
    if size > WHISPER_SIZE_LIMIT_BYTES:
        if bitrate_kbps <= 24:
            raise DubError(
                f"Аудіо для Whisper все одно завелике ({size / 1024 / 1024:.1f} МБ) "
                f"навіть на {bitrate_kbps}kbps — відео надто довге для одного запиту "
                f"до Whisper (потрібне розбиття на частини, зараз не реалізовано)."
            )
        print(f"[dub] аудіо {size / 1024 / 1024:.1f} МБ > 25 МБ, знижую бітрейт "
              f"{bitrate_kbps}kbps -> {bitrate_kbps // 2}kbps і перекодовую")
        return _extract_audio_for_whisper(video_path, out_path, bitrate_kbps // 2)
    return out_path


def transcribe(client: OpenAI, wav_path: str) -> tuple[str, list[dict]]:
    """Повертає (мова_повна_назва, [{"start","end","text"}, ...])."""
    with open(wav_path, "rb") as f:
        resp = client.audio.transcriptions.create(
            model="whisper-1", file=f, response_format="verbose_json",
        )

    language = getattr(resp, "language", "") or ""
    raw_segments = getattr(resp, "segments", None) or []

    segments = []
    for s in raw_segments:
        start = getattr(s, "start", None) if not isinstance(s, dict) else s.get("start")
        end = getattr(s, "end", None) if not isinstance(s, dict) else s.get("end")
        text = getattr(s, "text", None) if not isinstance(s, dict) else s.get("text")
        text = (text or "").strip()
        if text and start is not None and end is not None and end > start:
            segments.append({"start": float(start), "end": float(end), "text": text})

    return language, segments


# Дуже нарізаний монтаж (>100 коротких реплік) в одному запиті до GPT іноді
# губить/зливає пункти списку — перевірено на реальному відео (225
# сегментів -> 220 перекладів). Менші пачки суттєво надійніші.
TRANSLATE_BATCH_SIZE = 40


def _translate_batch(client: OpenAI, batch: list[dict], target_language: str) -> list[str]:
    import json

    numbered = [{"i": i, "text": seg["text"]} for i, seg in enumerate(batch)]

    system_prompt = (
        "Eres un traductor profesional de subtítulos/doblaje. Traduces del idioma "
        "original al español neutro, manteniendo el tono y la longitud aproximada "
        "de cada frase (para que encaje en el mismo tiempo de habla). No añadas ni "
        "quites información. No traduzcas el formato JSON, solo el campo 'text'."
    )
    user_prompt = (
        "Traduce cada elemento de esta lista al español. Devuelve ÚNICAMENTE un "
        "JSON con la clave 'translations': un array de strings, EXACTAMENTE en el "
        "mismo orden y con la misma cantidad de elementos que la entrada "
        f"({len(batch)} elementos, ni uno más ni uno menos).\n\n"
        f"Entrada:\n{json.dumps(numbered, ensure_ascii=False)}"
    )

    response = client.chat.completions.create(
        model="gpt-4.1",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.3,
    )

    data = json.loads(response.choices[0].message.content)
    translations = data.get("translations")
    if not isinstance(translations, list) or len(translations) != len(batch):
        raise DubError(
            f"GPT повернув {len(translations) if isinstance(translations, list) else 'не список'} "
            f"перекладів замість {len(batch)} (пачка)"
        )
    return [str(t) for t in translations]


def _translate_one(client: OpenAI, text: str, target_language: str) -> str:
    """Переклад одного рядка окремим запитом — гарантовано 1:1, без ризику
    злиття/втрати пунктів списку. Дорожче за пачку, тому лише як fallback."""
    response = client.chat.completions.create(
        model="gpt-4.1",
        messages=[
            {"role": "system", "content": (
                "Eres un traductor profesional de subtítulos/doblaje. Traduces al "
                "español neutro, manteniendo el tono y la longitud aproximada. "
                "Responde ÚNICAMENTE con la traducción, sin comillas ni comentarios."
            )},
            {"role": "user", "content": text},
        ],
        temperature=0.3,
    )
    return response.choices[0].message.content.strip()


def translate_segments(client: OpenAI, segments: list[dict], target_language: str = "es") -> list[str]:
    """Перекладає тексти сегментів пачками по TRANSLATE_BATCH_SIZE. Якщо
    пачка двічі поспіль повертає не ту кількість (реально трапляється на
    відео з дуже короткими сусідніми репліками) — перекладає її елемент за
    елементом окремими запитами, де розбіжність кількості неможлива."""
    translations: list[str] = []
    for start in range(0, len(segments), TRANSLATE_BATCH_SIZE):
        batch = segments[start:start + TRANSLATE_BATCH_SIZE]
        try:
            batch_translations = _translate_batch(client, batch, target_language)
        except DubError:
            print(f"[dub][WARN] пачка {start}-{start + len(batch)} невдала, повторюю спробу")
            try:
                batch_translations = _translate_batch(client, batch, target_language)
            except DubError:
                print(f"[dub][WARN] пачка {start}-{start + len(batch)} невдала вдруге, "
                      f"перекладаю по одному елементу")
                batch_translations = [_translate_one(client, seg["text"], target_language)
                                       for seg in batch]
        translations.extend(batch_translations)
    return translations


# Whisper ріже мову по паузах, тож один сегмент — це зазвичай одне коротке
# речення (1-3 с). Озвучувати такими шматками погано з двох причин:
# 1) іспанський переклад майже завжди довший за російський оригінал, і на
#    короткому сегменті тайм-стрейч мусить тиснути сильно — на слух це
#    "тараторення"; там, де переклад вийшов коротшим, навпаки розтягує — і
#    це "жування". На довшому куску надлишок і недостача сусідніх реплік
#    взаємно гасяться, і коефіцієнт тримається біля 1.0;
# 2) TTS на кожному шматку заново будує інтонацію з нуля, тож мова звучить
#    рублено, без наскрізної фрази.
# Ціна: синхронність із картинкою стає приблизною (±частка секунди).
# Ліп-синку тут все одно немає, тож це прийнятний розмін.
CHUNK_TARGET_SECONDS = 15.0
CHUNK_MAX_SECONDS = 22.0
# Пауза, довша за це, — природна межа думки; рвати там безпечно.
CHUNK_BREAK_GAP = 0.6
# А пауза, довша за це, — межа сцени; рвемо завжди, інакше кусок проковтне
# довгу тишу і вся арифметика тривалості попливе.
CHUNK_FORCE_GAP = 2.0


def _group_segments(segments: list[dict],
                    target_seconds: float = CHUNK_TARGET_SECONDS,
                    max_seconds: float = CHUNK_MAX_SECONDS,
                    break_gap: float = CHUNK_BREAK_GAP,
                    force_gap: float = CHUNK_FORCE_GAP) -> list[dict]:
    """Склеює сусідні сегменти Whisper у куски ~target_seconds.

    Розрив робиться лише в природних місцях: на достатньо довгій паузі
    після того, як кусок уже набрав цільову довжину, або примусово — на
    довгій тиші чи при перевищенні max_seconds.
    """
    chunks: list[dict] = []
    current: dict | None = None

    for seg in segments:
        if current is None:
            current = dict(seg)
            continue

        gap = seg["start"] - current["end"]
        span = seg["end"] - current["start"]
        filled = current["end"] - current["start"]

        if gap >= force_gap or span > max_seconds or (filled >= target_seconds and gap >= break_gap):
            chunks.append(current)
            current = dict(seg)
        else:
            current["end"] = seg["end"]
            current["text"] = f"{current['text']} {seg['text']}".strip()

    if current is not None:
        chunks.append(current)
    return chunks


# Скільки початку відео ігнорувати при виборі зразка голосу. Там майже
# завжди заставка, музична підводка або короткий вступ іншим голосом —
# саме через це клон міг вийти чужим (чоловік заговорив жіночим голосом).
VOICE_REF_SKIP_INTRO_SECONDS = float(os.environ.get("VOICE_REF_SKIP_INTRO_SECONDS", "25"))
# Пауза, довша за це, вважається межею між репліками різних людей або
# між студією і відеовставкою.
VOICE_REF_MAX_GAP = 1.0
# Ручний перехоплювач: якщо евристика все одно вибрала не того, можна
# вказати секунду, з якої різати зразок (env для конкретного прогону).
VOICE_REF_START_OVERRIDE = os.environ.get("VOICE_REF_START_SECONDS", "").strip()


def _longest_speech_run(segments: list[dict], skip_intro: float) -> list[dict]:
    """Найдовший безперервний фрагмент мови без великих пауз.

    Це найкраще наближення до "основного диктора", яке можна зробити без
    повноцінної діаризації: людина, що говорить у студії довше за всіх
    поспіль, майже завжди і є ведучим, а вставки та коментарі інших людей
    короткі й відокремлені паузами.
    """
    candidates = [s for s in segments if s["start"] >= skip_intro] or segments

    runs: list[list[dict]] = [[candidates[0]]]
    for seg in candidates[1:]:
        if seg["start"] - runs[-1][-1]["end"] > VOICE_REF_MAX_GAP:
            runs.append([seg])
        else:
            runs[-1].append(seg)

    return max(runs, key=lambda r: r[-1]["end"] - r[0]["start"])


def _build_voice_reference(wav_path: str, segments: list[dict], out_path: str) -> str:
    """Вирізає ~6-14 с чистої мови основного диктора для voice cloning.

    Раніше бралися просто перші секунди відео. На новинному матеріалі це
    систематично промахується: початок — це заставка, підводка або інший
    голос, і клон виходив не того, хто веде випуск.
    """
    if VOICE_REF_START_OVERRIDE:
        run = [s for s in segments if s["end"] > float(VOICE_REF_START_OVERRIDE)] or segments
        print(f"[dub] зразок голосу: ручний старт з {VOICE_REF_START_OVERRIDE} с")
    else:
        run = _longest_speech_run(segments, VOICE_REF_SKIP_INTRO_SECONDS)

    start = run[0]["start"]
    end = start
    used_texts = []
    for seg in run:
        if seg["start"] - start > VOICE_REF_MAX_SECONDS:
            break
        end = seg["end"]
        used_texts.append(seg["text"])
        if end - start >= VOICE_REF_MIN_SECONDS:
            break

    run_length = run[-1]["end"] - run[0]["start"]
    print(f"[dub] зразок голосу: {start:.1f}-{end:.1f} с "
          f"(з безперервного фрагмента {run_length:.1f} с, реплік у ньому {len(run)})")

    _run_ffmpeg(
        ["-i", wav_path, "-ss", str(start), "-to", str(end),
         "-acodec", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", out_path],
        "ffmpeg помилка вирізання voice reference",
    )
    return " ".join(used_texts)[:1000]


def _clone_voice(api_key: str, ref_path: str, transcript: str, lang_code: str, video_id: str) -> str:
    with open(ref_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode("ascii")

    payload = {
        "displayName": f"dub-{video_id}",
        "langCode": lang_code,
        "voiceSamples": [{"audioData": audio_b64, "transcription": transcript}],
        "description": "Auto voice clone для dub_pipeline (без ліп-синку)",
        "tags": ["auto-dub"],
        "audioProcessingConfig": {"removeBackgroundNoise": True},
    }
    headers = {"Authorization": f"Basic {api_key}", "Content-Type": "application/json"}

    resp = requests.post(f"{INWORLD_BASE_URL}/voices/v1/voices:clone",
                          json=payload, headers=headers, timeout=60)
    if resp.status_code >= 400:
        raise DubError(f"Inworld voice clone failed ({resp.status_code}): {resp.text}")

    data = resp.json()
    voice_id = (data.get("voice") or {}).get("voiceId") or data.get("voiceId")
    if not voice_id:
        raise DubError(f"Inworld не повернув voiceId: {data}")
    return voice_id


def _synthesize_segment(api_key: str, text: str, voice_id: str, target_language: str) -> bytes:
    """Повертає сирі PCM16 mono семпли (без WAV-заголовка).

    ВАЖЛИВО: параметр "language" (BCP-47, напр. "es") — це не косметика, а
    те, що фактично вмикає cross-lingual поведінку tts-2 (native-акцент
    цільової мови). Без нього перший реальний тест зберіг акцент оригіналу
    (голос синтезувався фонологією мови клонування, а не цільової).
    """
    payload = {
        "text": text,
        "voiceId": voice_id,
        "modelId": DEFAULT_MODEL_ID,
        "language": target_language,
        "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": SAMPLE_RATE},
    }
    headers = {"Authorization": f"Basic {api_key}", "Content-Type": "application/json"}

    resp = requests.post(f"{INWORLD_BASE_URL}/tts/v1/voice",
                          json=payload, headers=headers, timeout=60)
    if resp.status_code >= 400:
        raise DubError(f"Inworld TTS failed ({resp.status_code}): {resp.text}")

    data = resp.json()
    audio_content = data.get("audioContent") or data.get("result", {}).get("audioContent")
    if not audio_content:
        raise DubError(f"Inworld TTS не повернув audioContent: {data}")
    return base64.b64decode(audio_content)


def _write_wav(path: str, pcm_bytes: bytes) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_bytes)


def _read_wav_frames(path: str) -> bytes:
    with wave.open(path, "rb") as wf:
        return wf.readframes(wf.getnframes())


def _atempo_chain(factor: float) -> str:
    """ffmpeg atempo приймає лише 0.5-2.0 за один фільтр — розкладаємо на ланцюжок."""
    if factor <= 0:
        return "atempo=1.0"
    filters = []
    remaining = factor
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5
    filters.append(f"atempo={remaining:.6f}")
    return ",".join(filters)


FADE_MS = 8.0


def _apply_fade(pcm_bytes: bytes, fade_ms: float = FADE_MS) -> bytes:
    """Лінійний fade-in/fade-out на краях сегмента.

    Без цього кожен сегмент вставляється в тишу (чи впритул до сусіднього
    сегмента) з різким стрибком амплітуди на межі — звідси чутний
    клік/щиглик між репліками. Особливо критично там, де сусідні куски
    стикуються впритул, без паузи між ними.
    """
    fade_samples = min(int(SAMPLE_RATE * fade_ms / 1000), len(pcm_bytes) // 2 // 2)
    if fade_samples <= 0:
        return pcm_bytes

    samples = bytearray(pcm_bytes)
    total = len(samples) // 2
    for i in range(fade_samples):
        factor = i / fade_samples
        in_idx = i * 2
        val_in = struct.unpack_from("<h", samples, in_idx)[0]
        struct.pack_into("<h", samples, in_idx, int(val_in * factor))
        out_idx = (total - 1 - i) * 2
        val_out = struct.unpack_from("<h", samples, out_idx)[0]
        struct.pack_into("<h", samples, out_idx, int(val_out * factor))
    return bytes(samples)


# Межі тайм-стрейчу. За ними синтез перестає звучати як жива мова:
# стиснення понад ~1.15x чути як тараторення, розтягнення нижче ~0.92x — як
# жування. Раніше клемпа не було взагалі, тож на невдалому сегменті ffmpeg
# отримував коефіцієнт 1.6+ і результат був саме такий, як описав
# користувач. Краще розійтися з таймкодом оригіналу, ніж зіпсувати звук.
MAX_SPEEDUP = 1.15
# Розширено з 0.92: у прогоні #844 понад 40 кусків із 63 потребували темпу
# 0.63-0.90 і впирались у клемп, тобто синтез систематично коротший за
# оригінальну репліку — і різниця лишалась тишею. 0.85 дозволяє розтягнути
# трохи більше, не доводячи мову до "жування". Решту різниці прикриває
# фонова оригінальна доріжка (див. ORIGINAL_BED_VOLUME).
MAX_SLOWDOWN = 0.85


def _fit_to_duration(pcm_bytes: bytes, target_duration: float, work_dir: str, tag: str) -> bytes:
    """Підганяє тривалість сегмента під target_duration у межах клемпа.

    На відміну від попередньої версії НЕ обрізає хвіст: якщо після
    дозволеного стиснення кусок усе одно довший за ціль — повертається
    довшим. Обрізання по target_samples гарантовано з'їдало кінець фрази.
    Вирівнювання позиції — задача _assemble_dubbed_track.
    """
    current_duration = len(pcm_bytes) / 2 / SAMPLE_RATE
    if current_duration <= 0:
        return b""
    if target_duration <= 0:
        return pcm_bytes

    factor = current_duration / target_duration
    clamped = min(max(factor, MAX_SLOWDOWN), MAX_SPEEDUP)
    if abs(clamped - factor) > 0.01:
        print(f"[dub] {tag}: для точного тайму треба темп {factor:.2f}x, "
              f"обмежено до {clamped:.2f}x")

    # Майже без розтягування - не ганяємо через ffmpeg дарма.
    if 0.97 <= clamped <= 1.03:
        return pcm_bytes

    in_path = os.path.join(work_dir, f"{tag}_in.wav")
    out_path = os.path.join(work_dir, f"{tag}_out.wav")
    _write_wav(in_path, pcm_bytes)
    _run_ffmpeg(
        ["-i", in_path, "-filter:a", _atempo_chain(clamped),
         "-ar", str(SAMPLE_RATE), "-ac", "1", "-acodec", "pcm_s16le", out_path],
        f"ffmpeg помилка atempo для {tag}",
    )
    stretched = _read_wav_frames(out_path)
    for p in (in_path, out_path):
        if os.path.exists(p):
            os.remove(p)
    return stretched


def _assemble_dubbed_track(api_key: str, voice_id: str, segments: list[dict],
                            translations: list[str], total_duration: float,
                            out_wav_path: str, work_dir: str, target_language: str) -> None:
    """Збирає доріжку, рахуючи позицію за реально записаним аудіо.

    Раніше cursor просто стрибав на seg["end"], бо кожен сегмент силоміць
    підганявся під точну тривалість оригіналу (з обрізанням хвоста). Тепер
    сегмент може вийти довшим за свій таймкод, тож позиція рахується за
    довжиною буфера: якщо ми відстаємо, наступна репліка починається одразу
    і з'їдає паузу, замість того щоб тиснути темп.
    """
    buffer = bytearray()
    pairs = list(zip(segments, translations))

    for i, (seg, text) in enumerate(pairs):
        written = len(buffer) / 2 / SAMPLE_RATE
        if seg["start"] > written:
            buffer += b"\x00\x00" * round((seg["start"] - written) * SAMPLE_RATE)

        raw = _synthesize_segment(api_key, text, voice_id, target_language)

        start = max(seg["start"], written)
        # Скільки часу реально є до початку наступної репліки — паузу між
        # репліками краще витратити на мову, ніж тиснути темп.
        next_start = pairs[i + 1][0]["start"] if i + 1 < len(pairs) else total_duration
        available = max(next_start - start, 0.0)

        own = max(seg["end"] - start, 0.3)
        raw_duration = len(raw) / 2 / SAMPLE_RATE
        # Коротший за свій таймкод — тягнемо до таймкоду (в межах клемпа).
        # Довший — дозволяємо залізти в паузу, і лише за нею стискаємо.
        target = own if raw_duration <= own else min(raw_duration, max(own, available))

        fitted = _fit_to_duration(raw, target, work_dir, f"chunk{i}")
        buffer += _apply_fade(fitted)
        print(f"[dub] кусок {i + 1}/{len(pairs)} озвучено")

    written = len(buffer) / 2 / SAMPLE_RATE
    if written < total_duration:
        buffer += b"\x00\x00" * round((total_duration - written) * SAMPLE_RATE)
    elif written > total_duration + 0.5:
        # -shortest у _mux обріже хвіст по довжині відео. Логуємо, щоб таке
        # сповзання було видно в логах прогону, а не лише на слух.
        print(f"[dub][WARN] доріжка довша за відео на {written - total_duration:.1f} с — "
              f"кінець буде обрізано під довжину відеоряду")

    _write_wav(out_wav_path, bytes(buffer))


# Гучність оригінальної доріжки під дубляжем. 0.12 ≈ -18 dB: оригінал
# чутно як фон, але він не конкурує з перекладом.
#
# Навіщо взагалі лишати оригінал:
# 1) Там, де синтез коротший за репліку (а це більшість кусків — темп
#    постійно впирається в MAX_SLOWDOWN), раніше лишалась мертва тиша.
#    Тепер у цих проміжках чутно оригінал, і паузи перестають звучати як
#    обрив звуку.
# 2) Зберігається атмосфера: інтершум, музика, звуки подій у відеовставках,
#    які інакше зникали разом з оригінальною доріжкою.
ORIGINAL_BED_VOLUME = float(os.environ.get("ORIGINAL_BED_VOLUME", "0.12"))


def _mux(video_path: str, dubbed_wav_path: str, out_video_path: str) -> None:
    """Склеює відеоряд з дубляжем, підмішуючи оригінальне аудіо тихим фоном.

    normalize=0 обов'язковий: за замовчуванням amix ділить гучність на
    кількість входів, і дубляж став би вдвічі тихішим.
    """
    filter_complex = (
        f"[0:a]volume={ORIGINAL_BED_VOLUME}[bg];"
        f"[1:a]volume=1.0[fg];"
        f"[bg][fg]amix=inputs=2:duration=first:normalize=0[aout]"
    )
    _run_ffmpeg(
        ["-i", video_path, "-i", dubbed_wav_path,
         "-filter_complex", filter_complex,
         "-map", "0:v:0", "-map", "[aout]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-shortest", out_video_path],
        "ffmpeg помилка склейки фінального відео",
    )


def translate_video(openai_client: OpenAI, inworld_api_key: str, video_path: str,
                     work_dir: str, video_id: str, target_language: str = "es") -> str:
    """
    Повний DIY-дубляж без ліп-синку. Повертає шлях до готового mp4
    (той самий відеоряд, нова аудіодоріжка іспанською).
    """
    os.makedirs(work_dir, exist_ok=True)

    audio_path = os.path.join(work_dir, f"{video_id}_audio.mp3")
    _extract_audio_for_whisper(video_path, audio_path)

    language, segments = transcribe(openai_client, audio_path)
    if not segments:
        raise DubError("Whisper не розпізнав жодного сегмента мови у відео")
    print(f"[dub] Whisper: мова={language}, сегментів={len(segments)}")

    chunks = _group_segments(segments)
    avg = sum(c["end"] - c["start"] for c in chunks) / len(chunks)
    print(f"[dub] сегменти згруповано: {len(segments)} -> {len(chunks)} кусків "
          f"(середня довжина {avg:.1f} с)")

    translations = translate_segments(openai_client, chunks, target_language)

    ref_path = os.path.join(work_dir, f"{video_id}_voice_ref.wav")
    ref_transcript = _build_voice_reference(audio_path, segments, ref_path)

    if language.lower() == "ukrainian":
        print("[dub][WARN] Українська — не в GA-списку мов Inworld, "
              "якість клонування голосу не гарантована.")

    # ЕКСПЕРИМЕНТ (замість source_lang_code="ru"): клонуємо голос одразу з
    # langCode цільової мови. Перша спроба (модель tts-2 + "language": "es"
    # у /tts/v1/voice) акцент не прибрала — за документацією langCode при
    # клонуванні визначає саме "локаль" голосу (приклад з доків: щоб
    # клонувати голос із британським акцентом, шлють "en-GB"). Тобто
    # можливо, клон із langCode="ru" "зафіксовує" російську вимову вже на
    # цьому кроці, і жоден "language" на синтезі це не перекриває.
    # Якщо це теж не допоможе — це вже реальна межа tts-2, а не параметр,
    # який можна підкрутити.
    clone_lang_code = TARGET_LANG_CODE_MAP.get(target_language, target_language)
    voice_id = _clone_voice(inworld_api_key, ref_path, ref_transcript, clone_lang_code, video_id)
    print(f"[dub] Inworld voiceId: {voice_id} (клоновано з langCode={clone_lang_code}, "
          f"експеримент — раніше було мовою оригіналу)")

    total_duration = _ffprobe_duration(video_path)
    dubbed_wav_path = os.path.join(work_dir, f"{video_id}_dubbed_audio.wav")
    _assemble_dubbed_track(inworld_api_key, voice_id, chunks, translations,
                            total_duration, dubbed_wav_path, work_dir, target_language)

    output_path = os.path.join(work_dir, f"{video_id}_es.mp4")
    _mux(video_path, dubbed_wav_path, output_path)

    for p in (audio_path, ref_path, dubbed_wav_path):
        if os.path.exists(p):
            os.remove(p)

    return output_path
