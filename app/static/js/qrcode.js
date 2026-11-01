/* Minimal QR Code encoder — byte mode, error correction level M, versions 1–6
 * (up to 106 bytes: every deposit address fits). Written for this page so the
 * QR is generated in the browser with no image API and no third-party bundle.
 *
 * Follows ISO/IEC 18004: data bits, Reed–Solomon over GF(256), module
 * placement, the 8 data masks (best by penalty score) and the BCH format
 * information. Only byte mode and one ECC level are implemented — nothing
 * here needs more.
 */

const EXP = new Uint8Array(512);
const LOG = new Uint8Array(256);
(function () {
    let x = 1;
    for (let i = 0; i < 255; i++) {
        EXP[i] = x;
        LOG[x] = i;
        x <<= 1;
        if (x & 0x100) x ^= 0x11d;
    }
    for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
})();

function mul(a, b) {
    return a && b ? EXP[LOG[a] + LOG[b]] : 0;
}

// Per version, level M: total data codewords, ECC codewords per block, blocks.
const VERSIONS = [
    null,
    { data: 16, ecc: 10, blocks: 1 },
    { data: 28, ecc: 16, blocks: 1 },
    { data: 44, ecc: 26, blocks: 1 },
    { data: 64, ecc: 18, blocks: 2 },
    { data: 86, ecc: 24, blocks: 2 },
    { data: 108, ecc: 16, blocks: 4 },
];

function generator(degree) {
    let poly = [1];
    for (let i = 0; i < degree; i++) {
        const next = new Array(poly.length + 1).fill(0);
        for (let j = 0; j < poly.length; j++) {
            next[j] ^= poly[j];
            next[j + 1] ^= mul(poly[j], EXP[i]);
        }
        poly = next;
    }
    return poly;
}

function eccBytes(data, degree) {
    const gen = generator(degree);
    const rem = data.concat(new Array(degree).fill(0));
    for (let i = 0; i < data.length; i++) {
        const factor = rem[i];
        if (!factor) continue;
        for (let j = 0; j < gen.length; j++) rem[i + j] ^= mul(gen[j], factor);
    }
    return rem.slice(data.length);
}

function pickVersion(length) {
    const needed = length + 2; // 4-bit mode + 8-bit character count
    for (let v = 1; v < VERSIONS.length; v++) {
        if (VERSIONS[v].data >= needed) return v;
    }
    throw new Error('QR content too long');
}

function codewords(bytes, version) {
    const info = VERSIONS[version];
    const bits = [];
    const push = (value, len) => {
        for (let i = len - 1; i >= 0; i--) bits.push((value >>> i) & 1);
    };
    push(4, 4); // byte mode
    push(bytes.length, 8);
    for (const byte of bytes) push(byte, 8);

    const capacity = info.data * 8;
    for (let i = 0; i < Math.min(4, capacity - bits.length); i++) bits.push(0);
    while (bits.length % 8) bits.push(0);

    const data = [];
    for (let i = 0; i < bits.length; i += 8) {
        let value = 0;
        for (let j = 0; j < 8; j++) value = (value << 1) | bits[i + j];
        data.push(value);
    }
    let toggle = false;
    while (data.length < info.data) {
        data.push(toggle ? 0x11 : 0xec);
        toggle = !toggle;
    }

    const perBlock = info.data / info.blocks;
    const blocks = [];
    for (let b = 0; b < info.blocks; b++) {
        const chunk = data.slice(b * perBlock, (b + 1) * perBlock);
        blocks.push({ data: chunk, ecc: eccBytes(chunk, info.ecc) });
    }

    // Interleave data codewords, then ECC codewords.
    const out = [];
    for (let i = 0; i < perBlock; i++) for (const block of blocks) out.push(block.data[i]);
    for (let i = 0; i < info.ecc; i++) for (const block of blocks) out.push(block.ecc[i]);
    return out;
}

const MASKS = [
    (r, c) => (r + c) % 2 === 0,
    (r) => r % 2 === 0,
    (r, c) => c % 3 === 0,
    (r, c) => (r + c) % 3 === 0,
    (r, c) => (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0,
    (r, c) => ((r * c) % 2) + ((r * c) % 3) === 0,
    (r, c) => (((r * c) % 2) + ((r * c) % 3)) % 2 === 0,
    (r, c) => (((r + c) % 2) + ((r * c) % 3)) % 2 === 0,
];

function formatBits(mask) {
    // Level M = 0b00, then the 3-bit mask, then BCH(15,5) and the 0x5412 XOR.
    const data = mask;
    let rem = data << 10;
    for (let i = 14; i >= 10; i--) if ((rem >>> i) & 1) rem ^= 0x537 << (i - 10);
    return ((data << 10) | rem) ^ 0x5412;
}

function writeFormat(matrix, mask) {
    const size = matrix.length;
    const bits = formatBits(mask);
    for (let i = 0; i <= 5; i++) matrix[i][8] = (bits >>> i) & 1;
    matrix[7][8] = (bits >>> 6) & 1;
    matrix[8][8] = (bits >>> 7) & 1;
    matrix[8][7] = (bits >>> 8) & 1;
    for (let i = 9; i < 15; i++) matrix[8][14 - i] = (bits >>> i) & 1;
    for (let i = 0; i < 8; i++) matrix[8][size - 1 - i] = (bits >>> i) & 1;
    for (let i = 8; i < 15; i++) matrix[size - 15 + i][8] = (bits >>> i) & 1;
}

function penalty(matrix) {
    const size = matrix.length;
    let score = 0;
    const runScore = (run) => (run >= 5 ? 3 + (run - 5) : 0);

    for (let r = 0; r < size; r++) {
        let run = 1;
        for (let c = 1; c < size; c++) {
            if (matrix[r][c] === matrix[r][c - 1]) run++;
            else {
                score += runScore(run);
                run = 1;
            }
        }
        score += runScore(run);
    }
    for (let c = 0; c < size; c++) {
        let run = 1;
        for (let r = 1; r < size; r++) {
            if (matrix[r][c] === matrix[r - 1][c]) run++;
            else {
                score += runScore(run);
                run = 1;
            }
        }
        score += runScore(run);
    }

    for (let r = 0; r < size - 1; r++) {
        for (let c = 0; c < size - 1; c++) {
            const v = matrix[r][c];
            if (v === matrix[r][c + 1] && v === matrix[r + 1][c] && v === matrix[r + 1][c + 1])
                score += 3;
        }
    }

    const PATTERN_A = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0];
    const PATTERN_B = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1];
    const matches = (get, start) => {
        let a = true;
        let b = true;
        for (let k = 0; k < 11; k++) {
            const v = get(start + k);
            if (v !== PATTERN_A[k]) a = false;
            if (v !== PATTERN_B[k]) b = false;
        }
        return a || b;
    };
    for (let r = 0; r < size; r++) {
        for (let c = 0; c + 11 <= size; c++) {
            if (matches((i) => matrix[r][i], c)) score += 40;
        }
    }
    for (let c = 0; c < size; c++) {
        for (let r = 0; r + 11 <= size; r++) {
            if (matches((i) => matrix[i][c], r)) score += 40;
        }
    }

    let dark = 0;
    for (const row of matrix) for (const v of row) dark += v;
    score += Math.floor(Math.abs((dark * 100) / (size * size) - 50) / 5) * 10;
    return score;
}

export function qrMatrix(text) {
    const bytes = Array.from(new TextEncoder().encode(String(text)));
    const version = pickVersion(bytes.length);
    const size = 17 + 4 * version;
    const matrix = Array.from({ length: size }, () => new Array(size).fill(null));
    const fixed = Array.from({ length: size }, () => new Array(size).fill(false));

    const set = (r, c, value) => {
        if (r < 0 || c < 0 || r >= size || c >= size) return;
        matrix[r][c] = value;
        fixed[r][c] = true;
    };

    const finder = (top, left) => {
        for (let dr = -1; dr <= 7; dr++) {
            for (let dc = -1; dc <= 7; dc++) {
                const inside = dr >= 0 && dr <= 6 && dc >= 0 && dc <= 6;
                const dark =
                    inside &&
                    (dr === 0 ||
                        dr === 6 ||
                        dc === 0 ||
                        dc === 6 ||
                        (dr >= 2 && dr <= 4 && dc >= 2 && dc <= 4));
                set(top + dr, left + dc, dark ? 1 : 0);
            }
        }
    };
    finder(0, 0);
    finder(0, size - 7);
    finder(size - 7, 0);

    for (let i = 8; i < size - 8; i++) {
        set(6, i, i % 2 === 0 ? 1 : 0);
        set(i, 6, i % 2 === 0 ? 1 : 0);
    }

    const centers = [null, [], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34]][version];
    const last = centers[centers.length - 1];
    for (const r of centers) {
        for (const c of centers) {
            if ((r === 6 && c === 6) || (r === 6 && c === last) || (r === last && c === 6)) continue;
            for (let dr = -2; dr <= 2; dr++) {
                for (let dc = -2; dc <= 2; dc++) {
                    const ring = Math.max(Math.abs(dr), Math.abs(dc));
                    set(r + dr, c + dc, ring !== 1 ? 1 : 0);
                }
            }
        }
    }

    // Reserve both format-information copies before laying down data.
    for (let i = 0; i <= 8; i++) {
        if (matrix[i][8] === null) set(i, 8, 0);
        if (matrix[8][i] === null) set(8, i, 0);
    }
    for (let i = 0; i < 8; i++) {
        set(8, size - 1 - i, 0);
        set(size - 1 - i, 8, 0);
    }
    set(4 * version + 9, 8, 1); // dark module

    const bits = [];
    for (const value of codewords(bytes, version)) {
        for (let i = 7; i >= 0; i--) bits.push((value >>> i) & 1);
    }

    let index = 0;
    let row = size - 1;
    let direction = -1;
    for (let col = size - 1; col >= 1; col -= 2) {
        if (col === 6) col = 5;
        for (;;) {
            for (let k = 0; k < 2; k++) {
                const c = col - k;
                if (fixed[row][c]) continue;
                matrix[row][c] = index < bits.length ? bits[index] : 0;
                index++;
            }
            row += direction;
            if (row < 0 || row >= size) {
                row -= direction;
                direction = -direction;
                break;
            }
        }
    }

    let best = null;
    let bestScore = Infinity;
    for (let mask = 0; mask < 8; mask++) {
        const candidate = matrix.map((r) => r.slice());
        const test = MASKS[mask];
        for (let r = 0; r < size; r++) {
            for (let c = 0; c < size; c++) {
                if (!fixed[r][c] && test(r, c)) candidate[r][c] ^= 1;
            }
        }
        writeFormat(candidate, mask);
        const score = penalty(candidate);
        if (score < bestScore) {
            bestScore = score;
            best = candidate;
        }
    }
    return { size, matrix: best };
}

export function qrSvg(text, label = 'Deposit address QR code') {
    const { size, matrix } = qrMatrix(text);
    // ISO/IEC 18004 requires a 4-module quiet zone around the symbol. Scanners
    // are much less forgiving of a tight crop than they look, especially on
    // phone cameras in low light.
    const quiet = 4;
    const outer = size + quiet * 2;
    let path = '';
    for (let r = 0; r < size; r++) {
        for (let c = 0; c < size; c++) {
            if (matrix[r][c]) path += `M${c} ${r}h1v1h-1z`;
        }
    }
    return (
        `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${-quiet} ${-quiet} ${outer} ${outer}" ` +
        `width="200" height="200" shape-rendering="crispEdges" role="img" ` +
        `aria-label="${label}">` +
        `<rect x="${-quiet}" y="${-quiet}" width="${outer}" height="${outer}" fill="#ffffff"/>` +
        `<path d="${path}" fill="#111113"/></svg>`
    );
}
