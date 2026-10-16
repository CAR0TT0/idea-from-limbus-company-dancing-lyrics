"""QQ's variant DES, using precomputed permutations and S-box/P tables.

Adapted from LRC-GET (c) 2026 valenbine and QQMusicDecoder (c) 2023 WXRIW,
MIT licensed. Full notices: release/licenses/qq-qrc-MIT.txt.
Not standard DES and not intended for general cryptographic use.
"""
from concurrent.futures import CancelledError
import struct

_SBOXES = ((14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7, 0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8, 4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0, 15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13), (15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10, 3, 13, 4, 7, 15, 2, 8, 15, 12, 0, 1, 10, 6, 9, 11, 5, 0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15, 13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9), (10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8, 13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1, 13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7, 1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12), (7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15, 13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9, 10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4, 3, 15, 0, 6, 10, 10, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14), (2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9, 14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6, 4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14, 11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3), (12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11, 10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8, 9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6, 4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13), (4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1, 13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6, 1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2, 6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12), (13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7, 1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2, 7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8, 2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11))

_IP = (57,49,41,33,25,17,9,1,59,51,43,35,27,19,11,3,61,53,45,37,29,21,13,5,63,55,47,39,31,23,15,7,
       56,48,40,32,24,16,8,0,58,50,42,34,26,18,10,2,60,52,44,36,28,20,12,4,62,54,46,38,30,22,14,6)
_P = (15,6,19,20,28,11,27,16,0,14,22,25,4,17,30,9,1,7,23,13,31,26,2,8,18,12,29,5,21,10,3,24)
_C = (56,48,40,32,24,16,8,0,57,49,41,33,25,17,9,1,58,50,42,34,26,18,10,2,59,51,43,35)
_D = (62,54,46,38,30,22,14,6,61,53,45,37,29,21,13,5,60,52,44,36,28,20,12,4,27,19,11,3)
_COMPRESSION = (13,16,10,23,0,4,2,27,14,5,20,9,22,18,11,3,25,7,15,6,26,19,12,1,
                40,51,30,36,46,54,29,39,50,44,32,47,43,48,38,55,33,52,45,41,49,35,28,31)
_SHIFTS = (1,1,2,2,2,2,2,2,1,2,2,2,2,2,2,1)
_WORDS = struct.Struct('<II')


def _permute(value, width, positions):
    output = 0
    for position in positions:
        output = (output << 1) | ((value >> (width-1-position)) & 1)
    return output


def _byte_tables(positions):
    return tuple(tuple(_permute(value << (56-8*byte),64,positions) for value in range(256)) for byte in range(8))


_inverse = [0]*64
for _index, _position in enumerate(_IP):
    _inverse[_position] = _index
_IP_TABLES, _FP_TABLES = _byte_tables(_IP), _byte_tables(_inverse)
_SP = tuple(tuple(_permute(box[(v&32)|((v&31)>>1)|((v&1)<<4)] << (28-4*i),32,_P)
                 for v in range(64)) for i,box in enumerate(_SBOXES))


def _apply(value, table):
    return (table[0][value>>56] | table[1][(value>>48)&255] | table[2][(value>>40)&255] |
            table[3][(value>>32)&255] | table[4][(value>>24)&255] | table[5][(value>>16)&255] |
            table[6][(value>>8)&255] | table[7][value&255])


def _schedule(key):
    first,second = _WORDS.unpack(key)
    value = (first << 32) | second
    c,d = _permute(value,64,_C)<<4, _permute(value,64,_D)<<4
    schedule = []
    for shift in _SHIFTS:
        c = ((c<<shift) | (c>>(28-shift))) & 0xfffffff0
        d = ((d<<shift) | (d>>(28-shift))) & 0xfffffff0
        combined = 0
        for position in _COMPRESSION[:24]:
            combined = (combined<<1) | ((c>>(31-position)) & 1)
        for position in _COMPRESSION[24:]:
            combined = (combined<<1) | ((d>>(31-(position-27))) & 1)
        schedule.append(tuple((combined>>(42-6*i))&63 for i in range(8)))
    return tuple(schedule)


_KEY = b'!@#)(*$%123ZXC!@!@#)(NHL'
_K0,_K1,_K2 = (_schedule(_KEY[i:i+8]) for i in (0,8,16))
_DECRYPT = (_K2[::-1],_K1,_K0[::-1])


def decrypt_blocks(data, cancelled=lambda:False):
    if not data or len(data)%8:
        raise ValueError('QQ QRC 密文长度无效')
    output = bytearray(len(data))
    sp0,sp1,sp2,sp3,sp4,sp5,sp6,sp7 = _SP
    for offset in range(0,len(data),8):
        if offset%1024==0 and cancelled():
            raise CancelledError()
        first,second = _WORDS.unpack_from(data,offset)
        state = _apply((first<<32)|second,_IP_TABLES)
        left,right = state>>32,state&0xffffffff
        for keys in _DECRYPT:
            for k0,k1,k2,k3,k4,k5,k6,k7 in keys:
                f = (sp0[(((right&1)<<5)|(right>>27))^k0] |
                     sp1[((right>>23)&63)^k1] | sp2[((right>>19)&63)^k2] |
                     sp3[((right>>15)&63)^k3] | sp4[((right>>11)&63)^k4] |
                     sp5[((right>>7)&63)^k5] | sp6[((right>>3)&63)^k6] |
                     sp7[(((right&31)<<1)|(right>>31))^k7])
                left,right = right,left^f
            left,right = right,left
        result = _apply((left<<32)|right,_FP_TABLES)
        _WORDS.pack_into(output,offset,result>>32,result&0xffffffff)
    return bytes(output)
