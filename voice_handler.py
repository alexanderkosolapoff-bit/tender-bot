"""
Распознавание голосовых сообщений через OpenAI Whisper API.
"""
import os
import logging
import tempfile
from openai import OpenAI

logger = logging.getLogger(__name__)
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))


async def transcribe_voice(file_bytes: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as f:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="ru",
                response_format="text"
            )
        return result.strip()
    except Exception as e:
        logger.error(f"Ошибка Whisper: {e}")
        raise
    finally:
        os.remove(tmp_path)
