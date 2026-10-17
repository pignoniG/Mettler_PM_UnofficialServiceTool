"""Tiny 8051 assembler subset used by rom_temperature.py (copy of the encoder in the local rom_comparator.py)."""

DIRECT = {'B': 0xF0, 'DPL': 0x82, 'DPH': 0x83, 'ACC': 0xE0}
RN = ('R0', 'R1', 'R2', 'R3', 'R4', 'R5', 'R6', 'R7')


# ---------------------------------------------------------------- assembler
def encode(m, ops, pc, labels):
    def tgt(t): return labels[t] if labels is not None else pc
    def rel(t, size):
        d = tgt(t) - (pc + size)
        if labels is not None and not -128 <= d <= 127:
            raise ValueError(f"{m} to {t} out of range at {pc:04X} ({d})")
        return d & 0xFF
    imm = lambda o: isinstance(o, str) and o.startswith('#')
    iv = lambda o: int(o[1:], 0) & 0xFF
    def dirv(o):
        if isinstance(o, int): return o
        return DIRECT.get(o)
    o = list(ops)
    if m == 'MOV':
        d, s = o
        if d == 'A':
            if s in RN: return [0xE8 + RN.index(s)]
            if imm(s): return [0x74, iv(s)]
            if dirv(s) is not None: return [0xE5, dirv(s)]
        if s == 'A':
            if d in RN: return [0xF8 + RN.index(d)]
            if d == '@R0': return [0xF6]
            if dirv(d) is not None: return [0xF5, dirv(d)]
        if d == 'DPTR' and imm(s):
            v = int(s[1:], 0); return [0x90, v >> 8, v & 0xFF]
        if d == 'DPTR': return [0x90, tgt(s) >> 8, tgt(s) & 0xFF]
        if d in RN and imm(s): return [0x78 + RN.index(d), iv(s)]
        if d in RN and dirv(s) is not None: return [0xA8 + RN.index(d), dirv(s)]
        if dirv(d) is not None and s in RN: return [0x88 + RN.index(s), dirv(d)]
        if dirv(d) is not None and imm(s): return [0x75, dirv(d), iv(s)]
        if dirv(d) is not None and dirv(s) is not None: return [0x85, dirv(s), dirv(d)]
    if m == 'MOVX' and o == ['A', '@DPTR']: return [0xE0]
    if m == 'MOVX' and o == ['@DPTR', 'A']: return [0xF0]
    if m == 'INC':
        if o == ['DPTR']: return [0xA3]
        if o[0] in RN: return [0x08 + RN.index(o[0])]
        if o == ['A']: return [0x04]
    if m in ('ADD', 'ADDC', 'SUBB') and o[0] == 'A':
        base = {'ADD': 0x20, 'ADDC': 0x30, 'SUBB': 0x90}[m]
        s = o[1]
        if s in RN: return [base + 8 + RN.index(s)]
        if imm(s): return [base + 4, iv(s)]
        if dirv(s) is not None: return [base + 5, dirv(s)]
    if m == 'XRL' and o[0] == 'A' and imm(o[1]): return [0x64, iv(o[1])]
    if m == 'XRL' and o[0] == 'A' and o[1] in RN: return [0x68 + RN.index(o[1])]
    if m == 'ANL' and o[0] == 'A' and imm(o[1]): return [0x54, iv(o[1])]
    if m == 'CPL' and o == ['A']: return [0xF4]
    if m == 'RL' and o == ['A']: return [0x23]
    if m == 'RR' and o == ['A']: return [0x03]
    if m == 'RLC' and o == ['A']: return [0x33]
    if m == 'RRC' and o == ['A']: return [0x13]
    if m == 'MOVC' and o == ['A', '@A+DPTR']: return [0x93]
    if m == 'CLR' and o == ['A']: return [0xE4]
    if m == 'CLR' and o == ['C']: return [0xC3]
    if m == 'SETB' and o == ['C']: return [0xD3]
    if m == 'MUL': return [0xA4]
    if m == 'DIV': return [0x84]
    if m == 'CJNE' and o[0] == 'A' and imm(o[1]): return [0xB4, iv(o[1]), rel(o[2], 3)]
    if m == 'CJNE' and o[0] == 'A': return [0xB5, dirv(o[1]), rel(o[2], 3)]
    if m in ('JZ', 'JNZ', 'JC', 'JNC', 'SJMP'):
        return [{'JZ': 0x60, 'JNZ': 0x70, 'JC': 0x40, 'JNC': 0x50, 'SJMP': 0x80}[m], rel(o[0], 2)]
    if m in ('JB', 'JNB') and o[0] == 'ACC.7':
        return [0x20 if m == 'JB' else 0x30, 0xE7, rel(o[1], 3)]
    if m == 'DJNZ' and o[0] in RN: return [0xD8 + RN.index(o[0]), rel(o[1], 2)]
    if m == 'DJNZ' and dirv(o[0]) is not None: return [0xD5, dirv(o[0]), rel(o[1], 3)]
    if m == 'LJMP': return [0x02, tgt(o[0]) >> 8, tgt(o[0]) & 0xFF]
    if m == 'RET': return [0x22]
    if m == 'DB': return list(o)
    raise ValueError(f"cannot encode {m} {ops}")
