#!/usr/bin/env python3
"""
V10.45 cassette ROM: send the balance's temperature count over the serial interface.

The balance measures a temperature count continuously for its temperature compensation
(the number the service keypad shows as "297904 T").  The routine at C887 (called once per
weighing pass from 827B) low-pass filters it into XRAM 7047..7049 (24 bit, little endian);
the compensation reads it from there.

This patch hooks that call.  In continuous-output mode (I-FACE "S" = cONT) and when the
interface is free, it sends one extra line every --every passes:

    T 297904<CR><LF>

through the firmware's own character routine D6FC, so the line ending, pause bytes and parity
follow the balance's interface settings.  The weight lines are unchanged.  Other modes
(stable only / all / auto) send nothing extra.

Goes at E800 (free filler) and can be applied on top of any V10.45-based image, e.g. the
comparator ROM.  Counter at XRAM 70E8, register save area 70E9..70ED.

Usage:  rom_temperature.py IN.BIN OUT.BIN [--every 62]
"""
import argparse
import asm51

CODE_AT = 0xE800
CHECKSUM_AT = 0xF7FF
HOOK_AT, HOOK_OLD = 0x827B, bytes([0x12, 0xC8, 0x87])      # LCALL C887
TEMP = 0x7047                                              # 24 bit filtered temperature count
CNT, SAVE = 0x70E8, 0x70E9
PUTC = 0xD6FC                                              # firmware character output
PSW = 0xD0


def encode(m, ops, pc, labels):
    o = list(ops)
    def rel(t, size):
        if labels is None: return 0
        d = labels[t] - (pc + size)
        if not -128 <= d <= 127: raise ValueError(f"{m} to {t} out of range at {pc:04X}")
        return d & 0xFF
    if m == 'LCALL': return [0x12, o[0] >> 8, o[0] & 0xFF]
    if m in ('JB', 'JNB'): return [0x20 if m == 'JB' else 0x30, o[0], rel(o[1], 3)]
    if m == 'PUSH': return [0xC0, o[0]]
    if m == 'POP': return [0xD0, o[0]]
    if m == 'SJMP' and False: pass
    return asm51.encode(m, ops, pc, labels)


def assemble(org, prog):
    labels, pc = {}, org
    for lab, m, *ops in prog:
        if lab: labels[lab] = pc
        pc += len(encode(m, ops, pc, None))
    code, pc = [], org
    for lab, m, *ops in prog:
        b = encode(m, ops, pc, labels); code += b; pc += len(b)
    return bytes(code), labels


def program(every):
    P = []
    def I(*ins): P.append((None,) + ins)
    def L(label, *ins): P.append((label,) + ins)
    def dptr(v): I('MOV', 'DPTR', '#%d' % v)
    def put(ch): I('MOV', 'A', '#%d' % ch); I('LCALL', PUTC)

    I('LCALL', 0xC887)                                      # what the original call did
    dptr(0x7601); I('MOVX', 'A', '@DPTR'); I('ANL', 'A', '#192'); I('CJNE', 'A', '#192', 'DONE')   # cONT only
    for bit in (0x38, 0x7D, 0x78, 0x4B):                    # 27h.0 2Fh.5 2Fh.0 29h.3: output off / busy
        I('JB', bit, 'DONE')
    I('MOV', 'A', 0x35); I('CLR', 'C'); I('SUBB', 'A', '#64'); I('JNC', 'DONE')   # output buffer nearly full
    dptr(CNT); I('MOVX', 'A', '@DPTR'); I('INC', 'A'); I('MOVX', '@DPTR', 'A'); I('CJNE', 'A', '#%d' % every, 'DONE')
    I('CLR', 'A'); I('MOVX', '@DPTR', 'A'); I('LJMP', 'BODY')
    L('DONE', 'RET')
    L('BODY', 'PUSH', 0xF0); I('PUSH', PSW)                         # B, PSW
    dptr(SAVE)
    for r in ('R0', 'R1', 'R2', 'R3'):
        I('MOV', 'A', r); I('MOVX', '@DPTR', 'A'); I('INC', 'DPTR')
    dptr(TEMP)
    I('MOVX', 'A', '@DPTR'); I('MOV', 'R1', 'A'); I('INC', 'DPTR')
    I('MOVX', 'A', '@DPTR'); I('MOV', 'R2', 'A'); I('INC', 'DPTR')
    I('MOVX', 'A', '@DPTR'); I('MOV', 'R3', 'A')
    put(ord('T')); put(0x20)
    for k in (100000, 10000, 1000, 100, 10, 1):
        kl, km, kh = k & 255, (k >> 8) & 255, k >> 16
        tag = 'D%d' % k
        I('MOV', 'R0', '#0')
        L(tag, 'CLR', 'C')
        I('MOV', 'A', 'R1'); I('SUBB', 'A', '#%d' % kl); I('MOV', 'R1', 'A')
        I('MOV', 'A', 'R2'); I('SUBB', 'A', '#%d' % km); I('MOV', 'R2', 'A')
        I('MOV', 'A', 'R3'); I('SUBB', 'A', '#%d' % kh); I('MOV', 'R3', 'A')
        I('JC', 'B%d' % k); I('INC', 'R0'); I('SJMP', tag)
        L('B%d' % k, 'MOV', 'A', 'R1'); I('ADD', 'A', '#%d' % kl); I('MOV', 'R1', 'A')
        I('MOV', 'A', 'R2'); I('ADDC', 'A', '#%d' % km); I('MOV', 'R2', 'A')
        I('MOV', 'A', 'R3'); I('ADDC', 'A', '#%d' % kh); I('MOV', 'R3', 'A')
        I('MOV', 'A', 'R0'); I('ADD', 'A', '#48'); I('LCALL', PUTC)
    put(0x0D); put(0x0A)
    dptr(SAVE)
    for r in ('R0', 'R1', 'R2', 'R3'):
        I('MOVX', 'A', '@DPTR'); I('MOV', r, 'A'); I('INC', 'DPTR')
    I('POP', PSW); I('POP', 0xF0)
    I('RET')
    return P


def build(img, every=62):
    img = bytearray(img)
    if len(img) != 0x8000: raise ValueError("expected a 32 KiB ROM image")
    if sum(img) & 0xFF: raise ValueError("ROM checksum of the input is already wrong")
    if img[HOOK_AT - 0x8000:HOOK_AT - 0x8000 + 3] != HOOK_OLD:
        raise ValueError("unexpected bytes at 827B: not a V10.45 image")
    code, lab = assemble(CODE_AT, program(every))
    o = CODE_AT - 0x8000
    if any(b != 0xFF for b in img[o:o + len(code)]) or CODE_AT + len(code) > 0xF000:
        raise ValueError("patch area is not free filler")
    img[o:o + len(code)] = code
    img[HOOK_AT - 0x8000:HOOK_AT - 0x8000 + 3] = bytes([0x12, CODE_AT >> 8, CODE_AT & 0xFF])
    img[CHECKSUM_AT - 0x8000] = 0
    img[CHECKSUM_AT - 0x8000] = (-sum(img)) & 0xFF
    assert sum(img) & 0xFF == 0
    return bytes(img), code


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('inp'); p.add_argument('out')
    p.add_argument('--every', type=int, default=62, help='send a T line every N weighing passes (~0.16 s each)')
    a = p.parse_args()
    assert 1 <= a.every <= 255
    out, code = build(open(a.inp, 'rb').read(), a.every)
    open(a.out, 'wb').write(out)
    print(f"patch {CODE_AT:04X}-{CODE_AT + len(code) - 1:04X} ({len(code)} bytes), T line every {a.every} passes")
    print(f"written {a.out} (ROM checksum OK)")


if __name__ == '__main__':
    main()
