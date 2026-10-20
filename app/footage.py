import json
from pathlib import Path
import subprocess


def probe(path):
    result = subprocess.run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],
                            capture_output=True,text=True,timeout=30)
    if result.returncode:
        raise ValueError(f'FFprobe could not read {Path(path).name}: {result.stderr[-1500:]}')
    return json.loads(result.stdout)


def audio_duration(path):
    data = probe(path)
    audio = next((s for s in data['streams'] if s['codec_type']=='audio'),None)
    if not audio:
        raise ValueError('Downloaded file has no audio stream')
    duration = float(audio.get('duration') or data['format'].get('duration') or 0)
    if duration <= 0:
        raise ValueError('Audio is empty or has no measurable duration')
    return duration
