"""Generador QR Code puro en Python (Byte mode, ECC Level L, Versiones 1-6) -> PNG y ASCII."""
import io
from PIL import Image

# Tabla capacidad Byte ECC-L para versiones 1..6: (total_cw, data_cw, ecc_cw)
_SPEC_L = {
    1: (26, 19, 7),
    2: (44, 34, 10),
    3: (70, 55, 15),
    4: (100, 80, 20),
    5: (134, 108, 26),
    6: (172, 136, 36),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11d
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_gen(n_ecc):
    g = [1]
    for i in range(n_ecc):
        ng = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            ng[j] ^= c
            ng[j + 1] ^= _gf_mul(c, _EXP[i])
        g = ng
    return g


def _rs_encode(data, n_ecc):
    gen = _rs_gen(n_ecc)
    res = list(data) + [0] * n_ecc
    for i in range(len(data)):
        coef = res[i]
        if coef != 0:
            for j in range(1, len(gen)):
                res[i + j] ^= _gf_mul(gen[j], coef)
    return res[len(data):]


def _choose_version(n_bytes):
    for v in range(1, 7):
        if 2 + n_bytes <= _SPEC_L[v][1]:
            return v
    raise ValueError('URL demasiado larga para QR v1-v6 (max 134 bytes)')


def _encode_bytes(text):
    raw = text.encode('utf-8')
    ver = _choose_version(len(raw))
    _, data_cw, ecc_cw = _SPEC_L[ver]

    bits = []
    def push(val, n):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)

    push(0b0100, 4)  # Byte mode
    push(len(raw), 8)
    for b in raw:
        push(b, 8)
    # Terminator + pad to byte
    for _ in range(min(4, data_cw * 8 - len(bits))):
        bits.append(0)
    while len(bits) % 8 != 0:
        bits.append(0)

    cws = []
    for i in range(0, len(bits), 8):
        v = 0
        for b in bits[i:i + 8]:
            v = (v << 1) | b
        cws.append(v)
    pad = [0xec, 0x11]
    idx = 0
    while len(cws) < data_cw:
        cws.append(pad[idx % 2])
        idx += 1

    # Para versiones 5 y 6 ECC-L usan 2 bloques
    if ver in (5, 6):
        b1_len = data_cw // 2
        e_per = ecc_cw // 2
        b1, b2 = cws[:b1_len], cws[b1_len:]
        e1, e2 = _rs_encode(b1, e_per), _rs_encode(b2, e_per)
        out = []
        for i in range(b1_len):
            out.append(b1[i])
            out.append(b2[i])
        for i in range(e_per):
            out.append(e1[i])
            out.append(e2[i])
        return ver, out
    ecc = _rs_encode(cws, ecc_cw)
    return ver, cws + ecc


def build_qr_matrix(text):
    ver, codewords = _encode_bytes(text)
    size = 17 + 4 * ver
    mat = [[None] * size for _ in range(size)]
    func = [[False] * size for _ in range(size)]

    def set_func(r, c, val):
        if 0 <= r < size and 0 <= c < size:
            mat[r][c] = bool(val)
            func[r][c] = True

    def finder(r0, c0):
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = r0 + r, c0 + c
                if not (0 <= rr < size and 0 <= cc < size):
                    continue
                if r in (-1, 7) or c in (-1, 7):
                    set_func(rr, cc, False)
                elif r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4):
                    set_func(rr, cc, True)
                else:
                    set_func(rr, cc, False)

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    # Alignment patterns
    centers = _ALIGN[ver]
    for r in centers:
        for c in centers:
            if func[r][c]:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    set_func(r + dr, c + dc, max(abs(dr), abs(dc)) != 1)

    # Timing & dark module
    for i in range(8, size - 8):
        if not func[6][i]:
            set_func(6, i, i % 2 == 0)
        if not func[i][6]:
            set_func(i, 6, i % 2 == 0)
    set_func(4 * ver + 9, 8, True)

    # Reserve format info
    for i in range(9):
        if not func[8][i]:
            set_func(8, i, False)
        if not func[i][8]:
            set_func(i, 8, False)
    for i in range(8):
        set_func(8, size - 1 - i, False)
        if i < 7:
            set_func(size - 1 - i, 8, False)

    # Data bits
    all_bits = []
    for cw in codewords:
        for b in range(7, -1, -1):
            all_bits.append((cw >> b) & 1)
    bi = 0
    up = True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if up else range(size)
        for r in rows:
            for dc in (0, -1):
                cc = col + dc
                if not func[r][cc]:
                    bit = all_bits[bi] if bi < len(all_bits) else 0
                    bi += 1
                    # Mask 0: (r + c) % 2 == 0
                    mat[r][cc] = bool(bit ^ (((r + cc) % 2) == 0))
        up = not up
        col -= 2

    # Format info: ECC-L (01), Mask 0 (000) -> 01000 -> BCH(15,5) + XOR 101010000010010 = 111011111000100
    fmt = 0b111011111000100
    for i in range(6):
        v = bool((fmt >> (14 - i)) & 1)
        mat[8][i] = v
        mat[size - 1 - i][8] = v
    mat[8][7] = bool((fmt >> 8) & 1)
    mat[size - 7][8] = bool((fmt >> 8) & 1)
    mat[8][8] = bool((fmt >> 7) & 1)
    mat[7][8] = bool((fmt >> 6) & 1)
    for i in range(6):
        v = bool((fmt >> (5 - i)) & 1)
        mat[5 - i][8] = v
        mat[8][size - 6 + i] = v

    return mat


def qr_to_png_bytes(text, box=6, border=3):
    mat = build_qr_matrix(text)
    n = len(mat)
    img_sz = (n + border * 2) * box
    im = Image.new('RGB', (img_sz, img_sz), (255, 255, 255))
    px = im.load()
    for r in range(n):
        for c in range(n):
            if mat[r][c]:
                y0, x0 = (r + border) * box, (c + border) * box
                for dy in range(box):
                    for dx in range(box):
                        px[x0 + dx, y0 + dy] = (10, 14, 24)
    buf = io.BytesIO()
    im.save(buf, format='PNG')
    return buf.getvalue()


def qr_to_unicode(text):
    mat = build_qr_matrix(text)
    n = len(mat)
    pad = [[False] * (n + 2)] + [[False] + row + [False] for row in mat] + [[False] * (n + 2)]
    lines = []
    for r in range(0, len(pad), 2):
        top = pad[r]
        bot = pad[r + 1] if r + 1 < len(pad) else [False] * len(top)
        line = []
        for t, b in zip(top, bot):
            if t and b:
                line.append('█')
            elif t and not b:
                line.append('▀')
            elif not t and b:
                line.append('▄')
            else:
                line.append(' ')
        lines.append(''.join(line))
    return '\n'.join(lines)
