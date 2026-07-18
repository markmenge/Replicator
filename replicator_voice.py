from __future__ import annotations


def transcribe_prompt_with_whisper(*, seconds: int, model_name: str) -> str:
    try:
        import numpy as np
        import sounddevice as sd
        import whisper
    except Exception as exc:
        raise RuntimeError(
            "Whisper dependencies are missing. Install: pip install openai-whisper sounddevice"
        ) from exc

    if seconds < 1:
        raise RuntimeError("Voice capture duration must be at least 1 second")

    sample_rate = 16000
    frames = int(seconds * sample_rate)
    recording = sd.rec(frames, samplerate=sample_rate, channels=1, dtype="float32")
    sd.wait()

    audio = np.squeeze(recording)
    if audio.size == 0:
        return ""

    peak = float(np.max(np.abs(audio)))
    if peak < 0.005:
        return ""

    model = whisper.load_model(model_name)
    result = model.transcribe(audio, language="en", fp16=False)
    text = str(result.get("text", "")).strip()
    return text
