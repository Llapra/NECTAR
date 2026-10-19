"""Decodificacion de formatos de pixel GVSP.

El buffer que entrega el SDK es la carga util cruda tal como la manda la
camara. Aqui se traduce a algo que se pueda mostrar o guardar como imagen, sin
tocar nunca los datos que se graban: la grabacion guarda el crudo intacto.
"""

from __future__ import annotations

from typing import Tuple

import numpy as np

from ..devices.emergent.api import PixelFormat

_MONO8 = {PixelFormat.MONO8}
_MONO16 = {PixelFormat.MONO10, PixelFormat.MONO12}
_MONO_PACKED = {PixelFormat.MONO10_PACKED, PixelFormat.MONO12_PACKED}
_BAYER8 = {
    PixelFormat.BAYGB8, PixelFormat.BAYGR8,
    PixelFormat.BAYRG8, PixelFormat.BAYBG8,
}
_BAYER16 = {
    PixelFormat.BAYGB10, PixelFormat.BAYGR10, PixelFormat.BAYRG10, PixelFormat.BAYBG10,
    PixelFormat.BAYGB12, PixelFormat.BAYGR12, PixelFormat.BAYRG12, PixelFormat.BAYBG12,
}
_RGB8 = {PixelFormat.RGB8, PixelFormat.BGR8}


def bytes_per_pixel(fmt: PixelFormat) -> float:
    if fmt in _MONO8 or fmt in _BAYER8:
        return 1.0
    if fmt in _MONO16 or fmt in _BAYER16:
        return 2.0
    if fmt in _MONO_PACKED:
        return 1.5
    if fmt in _RGB8:
        return 3.0
    if fmt in (PixelFormat.RGB10, PixelFormat.RGB12,
               PixelFormat.BGR10, PixelFormat.BGR12):
        return 6.0
    if fmt == PixelFormat.YUV422_PACKED:
        return 2.0
    if fmt == PixelFormat.YUV444_PACKED:
        return 3.0
    if fmt == PixelFormat.YUV411_PACKED:
        return 1.5
    return 1.0


def significant_bits(fmt: PixelFormat) -> int:
    """Bits utiles por pixel (12 para Mono12, aunque viaje en 2 bytes)."""
    name = fmt.name
    if "12" in name:
        return 12
    if "10" in name:
        return 10
    return 8


def row_bytes(fmt: PixelFormat, width: int, stride: int = 0) -> int:
    """Bytes por fila, respetando el stride que reporte el SDK."""
    if stride and stride > 0:
        return int(stride)
    return int(round(width * bytes_per_pixel(fmt)))


def expected_size(fmt: PixelFormat, width: int, height: int, stride: int = 0) -> int:
    return row_bytes(fmt, width, stride) * int(height)


def _rows(raw: np.ndarray, width: int, height: int, fmt: PixelFormat,
          stride: int) -> np.ndarray:
    """Recorta el relleno de fila y devuelve un array (height, ancho_util)."""
    per_row = row_bytes(fmt, width, stride)
    useful = int(round(width * bytes_per_pixel(fmt)))
    needed = per_row * height
    if raw.size < needed:
        # Buffer mas corto de lo esperado: usar lo que haya.
        height = max(1, raw.size // per_row)
        needed = per_row * height
    block = raw[:needed].reshape(height, per_row)
    if useful < per_row:
        block = block[:, :useful]
    return block


def _unpack_12(block: np.ndarray, width: int) -> np.ndarray:
    """Mono12Packed -> uint16.

    Empaquetado GigE Vision: 2 pixeles en 3 bytes.
        byte0 = P0[11:4]
        byte1 = P1[3:0] << 4 | P0[3:0]
        byte2 = P1[11:4]
    """
    height = block.shape[0]
    triples = block.shape[1] // 3
    trio = block[:, : triples * 3].reshape(height, triples, 3).astype(np.uint16)

    b0 = trio[:, :, 0]
    b1 = trio[:, :, 1]
    b2 = trio[:, :, 2]

    p0 = (b0 << 4) | (b1 & 0x0F)
    p1 = (b2 << 4) | (b1 >> 4)

    out = np.empty((height, triples * 2), dtype=np.uint16)
    out[:, 0::2] = p0
    out[:, 1::2] = p1
    return out[:, :width]


def _unpack_10(block: np.ndarray, width: int) -> np.ndarray:
    """Mono10Packed -> uint16 (mismo esquema de 3 bytes por 2 pixeles)."""
    height = block.shape[0]
    triples = block.shape[1] // 3
    trio = block[:, : triples * 3].reshape(height, triples, 3).astype(np.uint16)

    b0 = trio[:, :, 0]
    b1 = trio[:, :, 1]
    b2 = trio[:, :, 2]

    p0 = (b0 << 2) | (b1 & 0x03)
    p1 = (b2 << 2) | ((b1 >> 4) & 0x03)

    out = np.empty((height, triples * 2), dtype=np.uint16)
    out[:, 0::2] = p0
    out[:, 1::2] = p1
    return out[:, :width]


def to_array(raw: np.ndarray, width: int, height: int, fmt: PixelFormat,
             stride: int = 0) -> np.ndarray:
    """Convierte el buffer crudo a un array 2D (mono) o 3D (color).

    Devuelve uint8 para formatos de 8 bits y uint16 para 10/12 bits.
    """
    block = _rows(raw, width, height, fmt, stride)

    if fmt in _MONO8 or fmt in _BAYER8:
        return block[:, :width]

    if fmt in _MONO16 or fmt in _BAYER16:
        return block.view(np.uint16)[:, :width]

    if fmt == PixelFormat.MONO12_PACKED:
        return _unpack_12(block, width)

    if fmt == PixelFormat.MONO10_PACKED:
        return _unpack_10(block, width)

    if fmt in _RGB8:
        img = block.reshape(block.shape[0], -1, 3)[:, :width, :]
        if fmt == PixelFormat.BGR8:
            img = img[:, :, ::-1]
        return img

    # Formato no contemplado: mostrar el primer plano de bytes.
    return block[:, :width]


def to_display_u8(raw: np.ndarray, width: int, height: int, fmt: PixelFormat,
                  stride: int = 0, step: int = 1,
                  auto_contrast: bool = False) -> np.ndarray:
    """Array uint8 contiguo listo para construir un QImage.

    `step` submuestrea por decimacion (step=2 -> la mitad de ancho y alto).
    Decimar antes de convertir es lo que mantiene barata la vista previa.
    """
    step = max(1, int(step))

    if fmt in _MONO_PACKED:
        # Los formatos empaquetados hay que desempaquetarlos completos: la
        # decimacion se aplica despues.
        img = to_array(raw, width, height, fmt, stride)
        if step > 1:
            img = img[::step, ::step]
    else:
        block = _rows(raw, width, height, fmt, stride)
        if fmt in _MONO16 or fmt in _BAYER16:
            img = block.view(np.uint16)[:, :width]
            img = img[::step, ::step]
        elif fmt in _RGB8:
            img = block.reshape(block.shape[0], -1, 3)[:, :width, :]
            img = img[::step, ::step, :]
            if fmt == PixelFormat.BGR8:
                img = img[:, :, ::-1]
        else:
            img = block[:, :width][::step, ::step]

    if img.dtype == np.uint16:
        if auto_contrast:
            img = _stretch(img)
        else:
            shift = max(0, significant_bits(fmt) - 8)
            img = (img >> shift).astype(np.uint8)
    elif auto_contrast and img.ndim == 2:
        img = _stretch(img)

    return np.ascontiguousarray(img, dtype=np.uint8)


def _stretch(img: np.ndarray) -> np.ndarray:
    """Estira el contraste entre los percentiles 1 y 99."""
    sample = img[::4, ::4] if img.size > 40000 else img
    lo = float(np.percentile(sample, 1.0))
    hi = float(np.percentile(sample, 99.0))
    if hi <= lo:
        hi = lo + 1.0
    scaled = (img.astype(np.float32) - lo) * (255.0 / (hi - lo))
    return np.clip(scaled, 0.0, 255.0).astype(np.uint8)


def to_savable(raw: np.ndarray, width: int, height: int, fmt: PixelFormat,
               stride: int = 0) -> Tuple[np.ndarray, int]:
    """Array para guardar como imagen, junto con sus bits utiles.

    Los formatos de mas de 8 bits se devuelven como uint16 con los datos
    alineados a la derecha (un Mono12 va de 0 a 4095), que es como los
    interpretan TIFF y PNG de 16 bits.
    """
    img = to_array(raw, width, height, fmt, stride)
    return img, significant_bits(fmt)
