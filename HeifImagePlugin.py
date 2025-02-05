import subprocess
import tempfile
from copy import copy
from weakref import WeakKeyDictionary

import piexif
import pyheif
from cffi import FFI
from PIL import Image, ImageFile
from pyheif.error import HeifError


try:
    from pyheif.transformations import Transformations
except ImportError:
    Transformations = None


ffi = FFI()
_keep_refs = WeakKeyDictionary()
HEIF_ENC_BIN = 'heif-enc'


def _crop_heif_file(heif):
    # Zero-copy crop before loading. Just shifts data pointer and updates meta.
    crop = heif.transformations.crop
    if crop == (0, 0) + heif.size:
        return heif

    if heif.mode not in ("L", "RGB", "RGBA"):
        raise ValueError("Unknown mode")
    pixel_size = len(heif.mode)

    offset = heif.stride * crop[1] + pixel_size * crop[0]
    cdata = ffi.from_buffer(heif.data, require_writable=False) + offset
    data = ffi.buffer(cdata, heif.stride * crop[3])

    # Keep reference to the original data as long as "cdata + offset" is alive.
    # Normally ffi.from_buffer should hold it for us but unfortunately
    # cdata + offset creates a new cdata object without reference.
    _keep_refs[cdata] = heif.data

    new_heif = copy(heif)
    new_heif.size = crop[2:4]
    new_heif.transformations = copy(heif.transformations)
    new_heif.transformations.crop = (0, 0) + crop[2:4]
    new_heif.data = data
    return new_heif


def _rotate_heif_file(heif):
    """
    Heif files already contain transformation chunks imir and irot which are
    dominate over Orientation tag in EXIF.

    This is not aligned with other formats behaviour and we MUST fix EXIF after
    loading to prevent unexpected rotation after resaving in other formats.

    And we come up to there is no reasons to force rotation of HEIF images
    after loading since we need update EXIF anyway.
    """
    orientation = heif.transformations.orientation_tag
    if not (1 <= orientation <= 8):
        return heif

    exif = {'0th': {piexif.ImageIFD.Orientation: orientation}}
    if heif.exif:
        try:
            exif = piexif.load(heif.exif)
            exif['0th'][piexif.ImageIFD.Orientation] = orientation
        except Exception:
            pass

    new_heif = copy(heif)
    new_heif.transformations = copy(heif.transformations)
    new_heif.transformations.orientation_tag = 0
    new_heif.exif = piexif.dump(exif)
    return new_heif


def _extract_heif_exif(heif_file):
    """
    Unlike other helper functions, this alters heif_file in-place.
    """
    heif_file.exif = None

    clean_metadata = []
    for item in heif_file.metadata or []:
        if item['type'] == 'Exif':
            if heif_file.exif is None:
                if item['data'] and item['data'][0:4] == b"Exif":
                    heif_file.exif = item['data']
        else:
            clean_metadata.append(item)
    heif_file.metadata = clean_metadata


class HeifImageFile(ImageFile.ImageFile):
    format = 'HEIF'
    format_description = "HEIF/HEIC image"

    def _open(self):
        try:
            heif_file = pyheif.open(
                self.fp, apply_transformations=Transformations is None)
        except HeifError as e:
            raise SyntaxError(str(e))

        _extract_heif_exif(heif_file)

        if Transformations is not None:
            heif_file = _rotate_heif_file(heif_file)
            self._size = heif_file.transformations.crop[2:4]
        else:
            self._size = heif_file.size

        if hasattr(self, "_mode"):
            self._mode = heif_file.mode
        else:
            # Fallback for Pillow < 10.1.0
            # https://pillow.readthedocs.io/en/stable/releasenotes/10.1.0.html#setting-image-mode
            self.mode = heif_file.mode

        if heif_file.exif:
            self.info['exif'] = heif_file.exif

        if heif_file.color_profile:
            # rICC is Restricted ICC. Still not sure can it be used.
            # ISO/IEC 23008-12 says: The colour information 'colr' descriptive
            # item property has the same syntax as the ColourInformationBox
            # as defined in ISO/IEC 14496-12.
            # ISO/IEC 14496-12 says: Restricted profile shall be of either
            # the Monochrome or Three‐Component Matrix‐Based class of
            # input profiles, as defined by ISO 15076‐1.
            # We need to go deeper...
            if heif_file.color_profile['type'] in ('rICC', 'prof'):
                self.info['icc_profile'] = heif_file.color_profile['data']

        self.tile = []
        self.heif_file = heif_file

    def load(self):
        heif_file, self.heif_file = self.heif_file, None
        if heif_file:
            try:
                heif_file = heif_file.load()
            except HeifError as e:
                cropped_file = e.code == 7 and e.subcode == 100
                if not cropped_file or not ImageFile.LOAD_TRUNCATED_IMAGES:
                    raise
                # Ignore EOF error and return blank image otherwise

            self.load_prepare()

            if heif_file.data:
                if Transformations is not None:
                    heif_file = _crop_heif_file(heif_file)
                self.frombytes(heif_file.data, "raw", (self.mode, heif_file.stride))

            heif_file.data = None

        return super().load()


def check_heif_magic(data):
    return pyheif.check(data) != pyheif.heif_filetype_no


def _save(im, fp, filename):
    # Save it before subsequent im.save() call
    info = im.encoderinfo

    if im.mode in ('P', 'PA'):
        # disbled due to errors in libheif encoder
        raise IOError("cannot write mode P as HEIF")

    with tempfile.NamedTemporaryFile(suffix='.png') as tmpfile:
        if im.mode == '1':
            # to circumvent `heif-enc` bug
            im = im.convert('L')
        im.save(
            tmpfile, format='PNG', optimize=False, compress_level=0,
            icc_profile=info.get('icc_profile', im.info.get('icc_profile')),
            exif=info.get('exif', im.info.get('exif'))
        )

        cmd = [HEIF_ENC_BIN, '-o', '/dev/stdout', tmpfile.name]

        avif = info.get('avif')
        if avif is None and filename:
            ext = filename.rpartition('.')[2].lower()
            avif = ext == 'avif'
        if avif:
            cmd.append('-A')

        if info.get('encoder'):
            cmd.extend(['-e', info['encoder']])

        if info.get('quality') is not None:
            cmd.extend(['-q', str(info['quality'])])

        if info.get('downsampling') is not None:
            if info['downsampling'] not in ('nn', 'average', 'sharp-yuv'):
                raise ValueError(f"Unknown downsampling: {info['downsampling']}")
            cmd.extend(['-C', info['downsampling']])

        subsampling = info.get('subsampling')
        if subsampling is not None:
            if subsampling == 0:
                subsampling = '444'
            elif subsampling == 1:
                subsampling = '422'
            elif subsampling == 2:
                subsampling = '420'
            cmd.extend(['-p', 'chroma=' + subsampling])

        if info.get('speed') is not None:
            cmd.extend(['-p', 'speed=' + str(info['speed'])])

        if info.get('concurrency') is not None:
            cmd.extend(['-p', 'threads=' + str(info['concurrency'])])

        try:
            # Warning: Do not open stdout and stderr at the same time
            with subprocess.Popen(cmd, stdout=subprocess.PIPE) as enc:
                for data in iter(lambda: enc.stdout.read(128 * 1024), b''):
                    fp.write(data)
                if enc.wait():
                    raise subprocess.CalledProcessError(enc.returncode, cmd)
        except FileNotFoundError:
            raise FileNotFoundError(
                2, f"Can't find heif encoding binary. Install '{HEIF_ENC_BIN}' "
                + "or set `HeifImagePlugin.HEIF_ENC_BIN` to full path.")


Image.register_open(HeifImageFile.format, HeifImageFile, check_heif_magic)
Image.register_save(HeifImageFile.format, _save)
Image.register_mime(HeifImageFile.format, 'image/heif')
Image.register_extensions(HeifImageFile.format, [".heic", ".avif"])

# Don't use this extensions for saving images, use the ones above.
# They have added for quick file type detection only (i.g. by Django).
Image.register_extensions(HeifImageFile.format, [".heif", ".hif"])
