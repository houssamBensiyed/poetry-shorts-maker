"""ASS progressive reveal with an identical full-sentence layout in every event.
Hidden words retain their layout space but have fully transparent fill AND outline.
"""
from pathlib import Path
from PIL import ImageFont


def layout(words, config, video):
    cap = config
    usable = video['width']-2*cap['safe_margin']-2*cap['outline_width']
    height = video['height']-2*cap['safe_margin']
    for size in range(cap['font_size'], 7, -1):
        font = ImageFont.truetype(cap['font_file'],size)
        lines, line = [], []
        for index,w in enumerate(words):
            candidate = line+[index]
            text = ' '.join(words[i].text for i in candidate)
            # Conservative width reserve for differences between Pillow and libass shaping.
            if line and (len(candidate)>cap['max_words_per_line'] or font.getlength(text)>usable*.9):
                lines.append(line)
                line = [index]
            else:
                line = candidate
        if line:
            lines.append(line)
        if (all(font.getlength(' '.join(words[i].text for i in line))<=usable*.9 for line in lines)
                and len(lines)*size*1.5 <= height):
            return size, lines
    raise ValueError('Caption cannot fit safe area, even at minimum font size')


def ass_time(seconds):
    cs = round(seconds*100)
    return f'{cs//360000}:{cs//6000%60:02d}:{cs//100%60:02d}.{cs%100:02d}'


def ass_color(color):
    return '&H00'+color[5:7]+color[3:5]+color[1:3]


def escape(text):
    # Prevent transcription from introducing ASS overrides or line breaks.
    return text.replace('\\','\\\u200b').replace('{','\\{').replace('}','\\}').replace('\n',' ').replace('\r',' ')


def write_ass(plan, cap, video, path):
    alignment = {'top':8,'center':5,'bottom':2}[cap['position']]
    x = video['width']/2
    y = {'top':cap['safe_margin'],'center':video['height']/2,'bottom':video['height']-cap['safe_margin']}[cap['position']]
    if any(ch in cap['font_name'] for ch in ',\r\n'):
        raise ValueError('Invalid font name')
    header = f'''[Script Info]
ScriptType: v4.00+
PlayResX: {video['width']}
PlayResY: {video['height']}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{cap['font_name']},{cap['font_size']},{ass_color(cap['color'])},{ass_color(cap['color'])},{ass_color(cap['outline_color'])},&HFF000000,0,0,0,0,100,100,0,0,1,{cap['outline_width']},0,{alignment},{cap['safe_margin']},{cap['safe_margin']},{cap['safe_margin']},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    events = []
    for caption in plan.captions:
        words = caption.sentence.words
        size, lines = layout(words,cap,video)
        for i,w in enumerate(words):
            end = words[i+1].start if i+1<len(words) else caption.end
            if ass_time(w.start) == ass_time(end):
                continue  # Simultaneous reveals are represented by the following cumulative event.
            text_lines = []
            for line in lines:
                text_lines.append(' '.join(('{\\alpha&H00&}' if j<=i else '{\\alpha&HFF&}')+escape(words[j].text) for j in line))
            text = f'{{\\an{alignment}\\pos({x},{y})\\fs{size}\\q2}}'+'\\N'.join(text_lines)
            events.append(f'Dialogue: 0,{ass_time(w.start)},{ass_time(end)},Default,,0,0,0,,{text}')
    Path(path).write_text(header+'\n'.join(events)+'\n',encoding='utf-8')
