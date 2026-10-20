from pathlib import Path
import copy
import os
import math
from urllib.parse import urlparse
import re
import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = {
    'baserow': {'api_url':'https://api.baserow.io','table_id':0,'images_table_id':0,'api_key_env':'BASEROW_API_TOKEN'},
    'downloads': {'timeout_seconds':60,'max_file_mb':256},
    'cloudinary': {'cloud_name':'','api_key_env':'CLOUDINARY_API_KEY','api_secret_env':'CLOUDINARY_API_SECRET',
                  'folder':'poetry-shorts','chunk_mb':10,'timeout_seconds':120},
    'whisper': {'model':'','api_url':'','api_key_env':'WHISPER_API_KEY','language':None,'word_timestamps':True,'timeout_seconds':120},
    'video': {'width':1080,'height':1920,'fps':30,'codec':'libx264','crf':20,'preset':'medium','threads':2},
    'footage': {'videos_directory':'footage/videos','images_directory':'footage/images','minimum_words_for_new_clip':3,'avoid_immediate_repetition':True,'random_seed':None,'use_local_assets':False},
    'captions': {'font_name':'Special Elite','font_file':'assets/fonts/SpecialElite-Regular.ttf','font_size':70,'color':'#FFFFFF','outline_color':'#000000','outline_width':3,'position':'center','max_words_per_line':5,'safe_margin':80,'animation':'word_by_word','persist_during_silence':True},
    'paths': {'output':'output','temp':'temp','logs':'logs','buffer':'buffer/voiceovers','image_buffer':'buffer/images'},
    'server': {'host':'127.0.0.1','port':8000},
}


def load_config(path=None):
    path = Path(path or ROOT/'config.yaml').resolve()
    try:
        with path.open() as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f'Invalid YAML syntax: {exc}') from exc
    if not isinstance(raw, dict):
        raise ValueError('config.yaml must contain named sections')
    config = copy.deepcopy(DEFAULTS)
    for section, values in raw.items():
        if section not in config or not isinstance(values,dict):
            raise ValueError(f'Unknown or invalid configuration section: {section}')
        if set(values)-set(config[section]):
            raise ValueError(f'Unknown setting in {section}: {set(values)-set(config[section])}')
        config[section].update(values)
    for section, keys in [('footage',['videos_directory','images_directory']),
                          ('captions',['font_file']),('paths',['output','temp','logs','buffer','image_buffer'])]:
        for key in keys:
            if not isinstance(config[section][key],str) or not config[section][key].strip():
                raise ValueError(f'{section}.{key} must be a nonempty path')
            config[section][key] = str((path.parent / config[section][key]).resolve())
    validate_config(config)
    return config


def validate_config(c):
    for section, keys in [('video',['width','height','fps','threads']),('footage',['minimum_words_for_new_clip']),
                          ('captions',['font_size','max_words_per_line','safe_margin'])]:
        for key in keys:
            v = c[section][key]
            if type(v) is not int or v <= 0:
                raise ValueError(f'{section}.{key} must be a positive integer')
    for section,keys in [('baserow',['api_url','api_key_env']),
                         ('whisper',['model','api_url','api_key_env']),('cloudinary',['cloud_name','api_key_env','api_secret_env','folder']),('captions',['font_name']),('server',['host'])]:
        for key in keys:
            if not isinstance(c[section][key],str):
                raise ValueError(f'{section}.{key} must be text (quote numeric-looking IDs)')
    for section in ('baserow','whisper'):
        endpoint = urlparse(c[section]['api_url'])
        if c[section]['api_url'] and (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
            raise ValueError(f'{section}.api_url must be an HTTPS endpoint without credentials, query, or fragment')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',c[section]['api_key_env']):
            raise ValueError(f'{section}.api_key_env must name an environment variable')
    if not c['baserow']['api_url']:
        raise ValueError('baserow.api_url cannot be empty')
    for field in ('table_id','images_table_id'):
        if type(c['baserow'][field]) is not int or c['baserow'][field]<0:
            raise ValueError(f'baserow.{field} must be a numeric table ID (0 means not configured yet)')
    if type(c['footage']['use_local_assets']) is not bool:
        raise ValueError('footage.use_local_assets must be true or false')
    for key in ('timeout_seconds','max_file_mb'):
        if type(c['downloads'][key]) is not int or c['downloads'][key]<=0:
            raise ValueError(f'downloads.{key} must be a positive integer')
    cloud = c['cloudinary']
    if cloud['cloud_name'] and not re.fullmatch(r'[A-Za-z0-9_-]+',cloud['cloud_name']):
        raise ValueError('cloudinary.cloud_name must be the account cloud name, not a URL')
    if cloud['folder'] and (not re.fullmatch(r'[A-Za-z0-9_/-]+',cloud['folder']) or '//' in cloud['folder']):
        raise ValueError('cloudinary.folder may contain letters, numbers, underscore, hyphen and slash')
    for field in ('api_key_env','api_secret_env'):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',cloud[field]):
            raise ValueError(f'cloudinary.{field} must name an environment variable')
    if type(cloud['chunk_mb']) is not int or not 5 <= cloud['chunk_mb'] <= 20:
        raise ValueError('cloudinary.chunk_mb must be between 5 and 20')
    if type(cloud['timeout_seconds']) is not int or cloud['timeout_seconds']<=0:
        raise ValueError('cloudinary.timeout_seconds must be a positive integer')
    timeout = c['whisper']['timeout_seconds']
    if not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('whisper.timeout_seconds must be positive and finite')
    if c['whisper']['language'] is not None and not isinstance(c['whisper']['language'],str):
        raise ValueError('whisper.language must be a language code or null')
    if not c['server']['host'] or not c['captions']['font_name']:
        raise ValueError('Server host and font name cannot be empty')
    v, cap = c['video'], c['captions']
    if v['width']%2 or v['height']%2 or v['codec'] != 'libx264':
        raise ValueError('Use even video dimensions and libx264 for H.264 output')
    if type(v['crf']) is not int or not 0 <= v['crf'] <= 51:
        raise ValueError('video.crf must be between 0 and 51')
    if v['preset'] not in {'ultrafast','superfast','veryfast','faster','fast','medium','slow','slower','veryslow'}:
        raise ValueError('Invalid x264 preset')
    if cap['position'] not in {'top','center','bottom'}:
        raise ValueError('captions.position must be top, center or bottom')
    for key in ['color','outline_color']:
        if not isinstance(cap[key],str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',cap[key]):
            raise ValueError(f'captions.{key} must be #RRGGBB')
    if not isinstance(cap['outline_width'], (int,float)) or not 0 <= cap['outline_width'] <= 20:
        raise ValueError('Caption outline must be between 0 and 20')
    if cap['safe_margin']*2 >= min(v['width'],v['height']):
        raise ValueError('Caption safe margins leave no space for text')
    if cap['animation'] != 'word_by_word' or cap['persist_during_silence'] is not True:
        raise ValueError('This version requires word_by_word captions with silence persistence')
    if c['whisper']['word_timestamps'] is not True:
        raise ValueError('Whisper word timestamps must be enabled')
    if type(c['server']['port']) is not int or not 1 <= c['server']['port'] <= 65535:
        raise ValueError('server.port must be between 1 and 65535')
    if type(c['footage']['avoid_immediate_repetition']) is not bool:
        raise ValueError('avoid_immediate_repetition must be true or false')
    if c['footage']['random_seed'] is not None and type(c['footage']['random_seed']) is not int:
        raise ValueError('random_seed must be an integer or null')


def check_job_config(c):
    if not c['baserow']['table_id']:
        raise ValueError('Set baserow.table_id in config.yaml before processing')
    if not c['baserow']['images_table_id']:
        raise ValueError('Set baserow.images_table_id for the separate image table')
    if c['baserow']['images_table_id']==c['baserow']['table_id']:
        raise ValueError('Use separate Baserow tables for audio jobs and images')
    if not os.environ.get(c['baserow']['api_key_env']):
        raise ValueError(f'Set {c["baserow"]["api_key_env"]} securely in the environment')
    if not c['whisper']['api_url'] or not c['whisper']['model']:
        raise ValueError('Set whisper.api_url and whisper.model for your transcription provider')
    if not os.environ.get(c['whisper']['api_key_env']):
        raise ValueError(f'Set {c["whisper"]["api_key_env"]} securely in the environment for transcription')
    if not c['cloudinary']['cloud_name']:
        raise ValueError('Set cloudinary.cloud_name in config.yaml')
    for field in ('api_key_env','api_secret_env'):
        if not os.environ.get(c['cloudinary'][field]):
            raise ValueError(f'Set {c["cloudinary"][field]} securely in the environment for uploads')
    if not Path(c['captions']['font_file']).is_file():
        raise ValueError('Special Elite font is missing; see README font instructions')
