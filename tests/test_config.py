import pytest
from app.config import load_config

@pytest.mark.parametrize('text',[
    'video:\n  fps: 0',
    'video:\n  width: 321',
    'captions:\n  persist_during_silence: false',
    'captions:\n  color: red',
    'google:\n  credentials_file: null',
    'google:\n  spreadsheet_id: 123',
    'whisper:\n  word_timestamps: false',
    'unknown: {}',
    'video: [broken',
])
def test_invalid_configuration_has_clear_error(tmp_path,text):
    path=tmp_path/'config.yaml'
    path.write_text(text)
    with pytest.raises(ValueError): load_config(path)


def test_relative_paths_and_defaults(tmp_path):
    path=tmp_path/'config.yaml'
    path.write_text('{}')
    c=load_config(path)
    assert c['video']['fps']==30
    assert c['paths']['output']==str(tmp_path/'output')
    assert c['whisper']['api_key_env']=='WHISPER_API_KEY'
    assert c['whisper']['api_url']==''
