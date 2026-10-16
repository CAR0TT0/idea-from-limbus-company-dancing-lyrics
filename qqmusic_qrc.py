"""Bounded, offline reads of the verified Windows QQ QRC cache container."""
import html
import re
import xml.etree.ElementTree as ET
import zlib

from lrc import LyricDocument, LyricLine, TimedWord, OFFSET, parse_lrc
from qqmusic_des import decrypt_blocks

MAX_ENCRYPTED = 256 * 1024
MAX_TEXT = 2 * 1024 * 1024
_SEED = ((0x4a,0xd6,0xca,0x90,0x67,0xf7,0x52),(0x5e,0x95,0x23,0x9f,0x13,0x11,0x7e),
         (0x47,0x74,0x3d,0x90,0xaa,0x3f,0x51),(0xc6,0x09,0xd5,0x9f,0xfa,0x66,0xf9),
         (0xf3,0xd6,0xa1,0x90,0xa0,0xf7,0xf0),(0x1d,0x95,0xde,0x9f,0x84,0x11,0xf4),
         (0x0e,0x74,0xbb,0x90,0xbc,0x3f,0x92),(0x00,0x09,0x5b,0x9f,0x62,0x66,0xa1))
_LINE = re.compile(r'\[(\d+),(\d+)\]')
_WORD = re.compile(r'\((\d+),(\d+)\)')
PURE_MUSIC = ('纯音乐，请欣赏','纯音乐,请欣赏','此歌曲为没有填词的纯音乐')
_QQ_ATTRIBUTE = re.compile(r'(?P<head><Lyric_1\b[^<>]*?\bLyricContent=")(?P<content>.*)(?P<tail>"\s*/>\s*</LyricInfo>\s*</QrcInfos>\s*)\Z',re.S)


def _xml_content(text):
    # Some real QQ files contain unescaped quotation marks inside LyricContent.
    # Accept only the single, final Lyric_1 attribute in the known QQ envelope.
    # Revalidate the envelope after escaping that payload; never expand a DTD.
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)',text,re.I):
        raise ValueError('QQ 歌词 XML 不支持实体声明')
    match = _QQ_ATTRIBUTE.search(text)
    payload = None
    if match and len(re.findall(r'\bLyricContent\s*=',text))==1:
        # XML attribute normalization would turn lyric line breaks into spaces.
        payload = html.unescape(match['content'])
        text = text[:match.start('content')]+html.escape(payload,quote=True)+match['tail']
    try:
        tree = ET.fromstring(text)
    except ET.ParseError as error:
        raise ValueError('QQ 歌词 XML 无效') from error
    if payload is not None and tree.tag!='QrcInfos':
        raise ValueError('QQ 歌词 XML 包装无效')
    values = [element.attrib['LyricContent'] for element in tree.iter() if 'LyricContent' in element.attrib]
    if len(values)!=1:
        raise ValueError('QQ 歌词 XML 内容存在歧义')
    return payload if payload is not None else values[0]


def _unmask(data):
    output = bytearray(len(data))
    x,y,dx,index = -1,8,1,-1
    for offset,value in enumerate(data):
        while True:
            index += 1
            if x<0:
                dx,y,mask = 1,(8-y)%8,0xc3
            elif x>6:
                dx,y,mask = -1,7-y,0xd8
            else:
                mask = _SEED[y][x]
            x += dx
            if not (index==0x8000 or (index>0x8000 and (index+1)%0x8000==0)):
                break
        output[offset] = value^mask
    return bytes(output)


def decode_qrc(data, cancelled=lambda:False):
    if not 11 < len(data) <= MAX_ENCRYPTED:
        raise ValueError('QQ 歌词缓存为空或过大')
    prefix = _unmask(data[:64])
    newline = prefix.find(b'\n')
    if newline<0 or not re.fullmatch(rb'\[offset:[+-]?\d{1,6}\]',prefix[:newline].rstrip(b'\r')):
        raise ValueError('尚不支持此 QQ 歌词缓存格式')
    outer_offset = int(prefix[:newline].strip().removeprefix(b'[offset:').removesuffix(b']'))
    # The client's nonzero outer offset sign has not yet been verified against audio.
    # Fail closed to official LRC instead of silently producing misaligned lyrics.
    if outer_offset:
        raise ValueError('QQ 缓存含未验证的客户端偏移，使用官方歌词回退')
    plain = decrypt_blocks(_unmask(data)[newline+1:],cancelled)
    inflater = zlib.decompressobj()
    try:
        text = inflater.decompress(plain,MAX_TEXT+1)
    except zlib.error as error:
        raise ValueError('QQ 歌词缓存损坏或尚未写完') from error
    if len(text)>MAX_TEXT or inflater.unconsumed_tail or not inflater.eof:
        raise ValueError('QQ 歌词缓存解压内容过大或不完整')
    if len(inflater.unused_data)>7 or any(inflater.unused_data):
        raise ValueError('QQ 歌词缓存有未知尾部数据')
    return text.decode('utf-8-sig',errors='strict')


def parse_qrc(text):
    if len(text)>MAX_TEXT or '\x00' in text:
        raise ValueError('QQ 歌词缓存内容无效')
    if text.lstrip().startswith('<'):
        text = _xml_content(text)
    if any(marker in text for marker in PURE_MUSIC):
        return None
    matches = list(_LINE.finditer(text))
    if not matches:
        return _bounded(parse_lrc(text))
    if len(matches)>5000:
        raise ValueError('QQ 歌词句数过多')
    offset_match = OFFSET.search(text[:matches[0].start()])
    offset = int(offset_match[1]) if offset_match else 0
    if abs(offset)>10000:
        raise ValueError('QQ 歌词偏移超出支持范围')
    lines,warnings,total_words = [],[],0
    for index,match in enumerate(matches):
        start,duration = int(match[1]),int(match[2])
        body = text[match.end():matches[index+1].start() if index+1<len(matches) else len(text)].strip('\r\n')
        parts,words,cursor,length,valid = [],[],0,0,True
        for token in _WORD.finditer(body):
            fragment = body[cursor:token.start()]
            parts.append(fragment)
            word_start,word_duration = int(token[1]),int(token[2])
            if word_duration and fragment:
                words.append(TimedWord(word_start,word_start+word_duration,length,length+len(fragment)))
                valid &= start<=word_start<word_start+word_duration<=start+duration+20
            length += len(fragment)
            cursor = token.end()
        parts.append(body[cursor:])
        lyric = ''.join(parts)
        if start<0 or start+duration>86400000 or len(lyric)>2048:
            raise ValueError('QQ 歌词时间或长度超出支持范围')
        valid &= all(a.start_ms<=b.start_ms for a,b in zip(words,words[1:]))
        if not valid:
            words = []
            warnings.append(f'第 {index+1} 句字词时间无效，保留整句歌词')
        total_words += len(words)
        if total_words>50000:
            raise ValueError('QQ 字词时间数量过多')
        lines.append(LyricLine(start,lyric,tuple(words)))
    # Ambiguous simultaneous rows are still shown, but don't invent a word mapping.
    grouped = {}
    for line in lines:
        if line.start_ms in grouped:
            previous = grouped[line.start_ms]
            grouped[line.start_ms] = LyricLine(line.start_ms,previous.text+'\n'+line.text)
        else:
            grouped[line.start_ms] = line
    if not any(line.text.strip() for line in lines):
        raise ValueError('QQ 缓存没有可显示的歌词')
    return LyricDocument(sorted(grouped.values(),key=lambda line:line.start_ms),warnings,offset)


def _bounded(document):
    if len(document.lines)>5000 or any(len(line.text)>2048 or line.start_ms>86400000 for line in document.lines) or abs(document.offset_ms)>10000:
        raise ValueError('QQ 歌词缓存超出支持范围')
    return document
