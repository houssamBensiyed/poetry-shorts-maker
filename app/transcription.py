"""Hosted Whisper transcription. No model weights or local inference runtime."""
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import httpx
from app.timeline import Word, validate_words
from app.video_builder import check_cancel, run_ffmpeg

log = logging.getLogger('video_builder')


@dataclass(frozen=True)
class Transcript:
    words: tuple[Word, ...]
    segments: tuple[dict, ...]


def normalize_response(data, duration):
    """Restore punctuation from transcript text when the word array omits it.

    Alignment must match every recognized letter/digit; otherwise fail rather
    than inventing words or timestamps. Works across API segment boundaries.
    """
    text = data.get('text', '')
    raw = data.get('words') or []
    if not isinstance(text,str) or not isinstance(raw,list):
        raise ValueError('Invalid transcription API response')
    if text.strip() and not raw:
        raise ValueError('Transcription API returned speech without word timestamps')
    canonical, positions = [], []
    for index, char in enumerate(text):
        if char.isalnum():
            for normalized in char.casefold():
                canonical.append(normalized)
                positions.append(index)
    tokens = []
    for word in raw:
        token = ''.join(c.casefold() for c in word.get('word','') if c.isalnum())
        if not token or word.get('start') is None or word.get('end') is None:
            raise ValueError('Transcription API returned an invalid word or missing word timestamps')
        tokens.append(token)
    if ''.join(tokens) != ''.join(canonical):
        raise ValueError('Transcription text does not align with timestamped words; cannot safely restore punctuation')
    words, cursor, start_char = [], 0, 0
    for word, token in zip(raw,tokens):
        cursor += len(token)
        end_char = positions[cursor] if cursor<len(positions) else len(text)
        words.append(Word(text[start_char:end_char].strip(),float(word['start']),float(word['end'])))
        start_char = end_char
    validate_words(words,duration)
    segments = []
    for segment in data.get('segments') or []:
        start, end = float(segment['start']),float(segment['end'])
        if not 0 <= start <= end <= duration:
            raise ValueError('API segment timestamps are outside the audio duration')
        segments.append({'text':segment['text'],'start':start,'end':end})
    if words and not segments:
        raise ValueError('Transcription API did not return requested segment timestamps')
    return Transcript(tuple(words),tuple(segments))


class Transcriber:
    def __init__(self, config):
        self.config = config

    def transcribe(self, path, duration, stop):
        check_cancel(stop)
        if not self.config['api_url'] or not self.config['model']:
            raise ValueError('Set whisper.api_url and whisper.model for your transcription provider')
        key = os.environ.get(self.config['api_key_env'],'')
        if not key:
            raise ValueError(f'Set {self.config["api_key_env"]} securely in the environment for transcription')
        # Drive downloads have no reliable extension. A lossless ASR copy also
        # avoids unsupported input containers; the original remains the render audio.
        path = Path(path).resolve()
        upload = path.parent/'transcription.flac'
        run_ffmpeg(['-i',str(path),'-map','0:a:0','-vn','-ac','1','-ar','16000',
                    '-c:a','flac',str(upload)],path.parent,stop,duration,lambda _:None,'Prepare API audio')
        try:
            if upload.stat().st_size > 24_000_000:
                raise ValueError('Transcription audio exceeds the API upload limit (24 MB); use a shorter voiceover')
            fields = {'model':self.config['model'],'response_format':'verbose_json',
                      'timestamp_granularities[]':['word','segment']}
            if self.config['language']:
                fields['language'] = self.config['language']
            log.info('Transcribing with hosted API model %s',self.config['model'])
            with httpx.Client(timeout=self.config['timeout_seconds'],follow_redirects=False) as client:
                for attempt in range(4):
                    check_cancel(stop)
                    try:
                        with upload.open('rb') as audio:
                            response = client.post(self.config['api_url'],headers={'Authorization':f'Bearer {key}'},
                                data=fields,files={'file':('voiceover.flac',audio,'audio/flac')})
                        if response.status_code == 200:
                            check_cancel(stop)
                            return normalize_response(response.json(),duration)
                        if response.status_code not in {408,429,500,502,503,504}:
                            raise ValueError(f'Transcription API returned HTTP {response.status_code}; check endpoint, model, key and account access')
                        failure = f'HTTP {response.status_code}'
                    except (httpx.TimeoutException,httpx.TransportError) as exc:
                        failure = type(exc).__name__
                    if attempt == 3:
                        raise RuntimeError(f'Transcription API failed after 4 attempts ({failure})')
                    log.warning('Temporary transcription API failure (%s); retrying',failure)
                    stop.wait(2**attempt)
        finally:
            upload.unlink(missing_ok=True)
