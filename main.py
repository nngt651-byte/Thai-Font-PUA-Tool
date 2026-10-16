# -*- coding: utf-8 -*-
"""
Thai PUA Tool
=============
Option 1: Generate Thai PUA font from real Thai grapheme clusters.
Option 2: Convert Thai <-> PUA using mapping.json from Option 1.
Option 3: Simple manual Thai -> Unicode/PUA text converter using mapping.json.

Install:
    pip install fonttools uharfbuzz regex pillow

Run:
    python main_thai.py
"""

import os
import re
import json
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import regex
import uharfbuzz as hb
from fontTools.ttLib import TTFont
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from PIL import Image, ImageDraw, ImageFont


APP_NAME = "Thai PUA Tool — Thai Localization"
DEFAULT_PUA = 0xE000
MAX_PUA = 0xF8FF

# Thai block: U+0E00–U+0E7F
THAI_RE = re.compile(r"[\u0E00-\u0E7F]")

# Convert every consonant that has at least one mark attached: a vowel above/below
# (U+0E31, U+0E34-U+0E3A), SARA AM (U+0E33), or a tone mark (U+0E47-U+0E4E), together
# with all marks that follow it. Plain consonants with no mark (e.g. น), ะ, า, leading
# vowels (U+0E40-U+0E44) and lakkhangyao (U+0E45) stay as normal Thai text.
THAI_CLUSTER_RE = re.compile(
    r"[\u0E01-\u0E2E](?=[\u0E31\u0E33\u0E34-\u0E3A\u0E47-\u0E4E])[\u0E47-\u0E4E]*[\u0E31\u0E33\u0E34-\u0E3A\u0E47-\u0E4E]*"
)


# ============================================================
# GENERAL HELPERS
# ============================================================

def read_text(path: Path) -> str:
    """Read common Unicode text encodings safely."""
    for enc in ("utf-8-sig", "utf-8", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def collect_txt(folder: Path):
    if not folder.exists():
        return []
    return sorted(p for p in folder.rglob("*.txt") if p.is_file())


def thai_clusters(text: str):
    """Return Thai syllable-style clusters in reading order (may repeat)."""
    return THAI_CLUSTER_RE.findall(text)


def parse_codepoint(value) -> int:
    if isinstance(value, int):
        return value
    s = str(value).strip().upper()
    if s.startswith("U+"):
        return int(s[2:], 16)
    if s.startswith("0X"):
        return int(s[2:], 16)
    return int(s, 16)


def codepoint_name(cp: int) -> str:
    return f"U+{cp:04X}"


def validate_unicode_codepoint(cp: int):
    if not (0 <= cp <= 0x10FFFF):
        raise ValueError("Unicode codepoint must be between U+0000 and U+10FFFF.")
    if 0xD800 <= cp <= 0xDFFF:
        raise ValueError("UTF-16 surrogate codepoints are not valid Unicode scalar values.")


# ============================================================
# WINDOWS FONT AUTO DETECTION
# ============================================================

def windows_font_dirs():
    result = []
    windir = os.environ.get("WINDIR")
    if windir:
        result.append(Path(windir) / "Fonts")

    local = os.environ.get("LOCALAPPDATA")
    if local:
        result.append(Path(local) / "Microsoft" / "Windows" / "Fonts")

    return [p for p in result if p.exists()]


def find_fonts():
    result = []
    seen = set()

    for folder in windows_font_dirs():
        try:
            for p in folder.iterdir():
                if p.suffix.lower() in {".ttf", ".otf", ".ttc"}:
                    k = str(p).lower()
                    if k not in seen:
                        seen.add(k)
                        result.append(p)
        except OSError:
            pass

    return result


def font_name_normalized(path: Path):
    return path.name.lower().replace(" ", "").replace("-", "").replace("_", "")


def find_noto_thai():
    fonts = find_fonts()
    candidates = [p for p in fonts if "notosansthai" in font_name_normalized(p)]
    if not candidates:
        return None

    candidates.sort(key=lambda p: (
        "bold" in p.name.lower(),
        "italic" in p.name.lower(),
        "variable" in p.name.lower(),
        p.suffix.lower() == ".ttc",
        p.name.lower(),
    ))
    return candidates[0]


# ============================================================
# HARFBUZZ
# ============================================================

def load_hb_font(font_path: Path):
    data = font_path.read_bytes()
    face = hb.Face(data)
    font = hb.Font(face)
    font.scale = (face.upem, face.upem)
    return data, face, font


def shape_text(font_path: Path, text: str):
    _, face, font = load_hb_font(font_path)

    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()

    hb.shape(font, buf, {
        "kern": True,
        "liga": True,
        "clig": True,
        "calt": True,
        "mark": True,
        "mkmk": True,
    })

    result = []
    x = 0
    y = 0

    for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
        result.append({
            "gid": info.codepoint,
            "cluster": info.cluster,
            "x": x + pos.x_offset,
            "y": y + pos.y_offset,
            "x_advance": pos.x_advance,
            "y_advance": pos.y_advance,
        })
        x += pos.x_advance
        y += pos.y_advance

    return face, result


def gid_to_name(font: TTFont, gid: int):
    order = font.getGlyphOrder()
    if 0 <= gid < len(order):
        return order[gid]
    return None


# ============================================================
# OPTION 1
# ============================================================

def draw_glyph_with_offset(src_glyph_set, glyph_name, target_pen, ox, oy):
    glyph = src_glyph_set[glyph_name]

    class OffsetPen:
        def __init__(self, target, xoff, yoff):
            self.target = target
            self.xoff = xoff
            self.yoff = yoff

        def moveTo(self, p):
            self.target.moveTo((p[0] + self.xoff, p[1] + self.yoff))

        def lineTo(self, p):
            self.target.lineTo((p[0] + self.xoff, p[1] + self.yoff))

        def curveTo(self, *points):
            self.target.curveTo(*[
                (p[0] + self.xoff, p[1] + self.yoff) for p in points
            ])

        def qCurveTo(self, *points):
            converted = []
            for p in points:
                if p is None:
                    converted.append(None)
                else:
                    converted.append((p[0] + self.xoff, p[1] + self.yoff))
            self.target.qCurveTo(*converted)

        def closePath(self):
            self.target.closePath()

        def endPath(self):
            self.target.endPath()

        def addComponent(self, component_name, transformation):
            a, b, c, d, e, f = transformation
            self.target.addComponent(
                component_name,
                (a, b, c, d, e + self.xoff, f + self.yoff)
            )

    # Standard fontTools transform: translates points AND composite-glyph
    # components correctly, so each shaped glyph keeps its real outline shape.
    glyph.draw(TransformPen(target_pen, (1, 0, 0, 1, ox, oy)))


def base_side_bearings(source_font, glyph_name):
    """Left/right side bearing of a source glyph (font units), never negative."""
    if not glyph_name or glyph_name not in source_font["hmtx"].metrics:
        return 0, 0
    advance, _ = source_font["hmtx"][glyph_name]
    g = source_font["glyf"][glyph_name]
    g.recalcBounds(source_font["glyf"])
    if g.numberOfContours == 0 or g.xMin is None:
        return 0, 0
    return max(0, g.xMin), max(0, advance - g.xMax)


def build_cluster_glyph(src_glyph_set, source_font, shaped, dx):
    """Draw all shaped glyphs of one cluster into a single TrueType glyph.
    dx shifts the whole cluster horizontally. Returns (glyph, total_advance)."""
    pen = TTGlyphPen(src_glyph_set)
    total_advance = 0

    for item in shaped:
        source_name = gid_to_name(source_font, item["gid"])
        if not source_name:
            continue

        draw_glyph_with_offset(
            src_glyph_set,
            source_name,
            pen,
            item["x"] + dx,
            item["y"],
        )
        total_advance += item["x_advance"]

    return pen.glyph(), total_advance


def letter_advance(source_font, shaped):
    """Sum of advances of non-mark glyphs in a shaped cluster (font units)."""
    class_defs = None
    if "GDEF" in source_font and source_font["GDEF"].table.GlyphClassDef:
        class_defs = source_font["GDEF"].table.GlyphClassDef.classDefs
    total = 0
    for item in shaped:
        name = gid_to_name(source_font, item["gid"])
        if class_defs is not None and class_defs.get(name) == 3:
            continue  # mark: no width
        total += item["x_advance"]
    return total


def letter_ink_center(source_font, shaped):
    """Horizontal centre of the letter ink (non-mark glyphs only), font units, or None."""
    glyf = source_font["glyf"]
    class_defs = None
    if "GDEF" in source_font and source_font["GDEF"].table.GlyphClassDef:
        class_defs = source_font["GDEF"].table.GlyphClassDef.classDefs
    xs = []
    for item in shaped:
        name = gid_to_name(source_font, item["gid"])
        if not name:
            continue
        if class_defs is not None and class_defs.get(name) == 3:
            continue  # mark: not part of the letter
        g = glyf[name]
        g.recalcBounds(glyf)
        if g.numberOfContours == 0 or g.xMin is None:
            continue
        xs.append(item["x"] + g.xMin)
        xs.append(item["x"] + g.xMax)
    if not xs:
        return None
    return (min(xs) + max(xs)) / 2


# Latin characters (ASCII, Latin-1, common punctuation) that must exist in the output font.
LATIN_CODEPOINTS = (
    list(range(0x20, 0x7F)) + list(range(0xA0, 0x100))
    + [0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2026, 0x20AC]
)


def find_fallback_latin():
    """Find a Latin TrueType font on Windows to fill in missing English glyphs."""
    for p in find_fonts():
        if font_name_normalized(p) in {"arial.ttf", "segoeui.ttf", "tahoma.ttf"}:
            return p
    return None


def add_missing_latin(out_font, out_order, fallback_path, log):
    """Copy Latin glyphs that the Thai source font lacks from a TrueType fallback font.
    Glyphs are decomposed and scaled to the Thai font's units-per-em. Existing
    codepoints are never replaced. Updates out_order in place."""
    fb = TTFont(str(fallback_path))
    if "glyf" not in fb or "cmap" not in fb:
        raise RuntimeError("The Latin fallback font must be a TrueType (.ttf) font.")

    fb_cmap = fb.getBestCmap()
    out_cmap = out_font.getBestCmap()
    scale = out_font["head"].unitsPerEm / fb["head"].unitsPerEm
    fb_glyphs = fb.getGlyphSet()
    out_glyf = out_font["glyf"]
    out_hmtx = out_font["hmtx"]

    added = []
    for cp in LATIN_CODEPOINTS:
        if cp in out_cmap or cp not in fb_cmap:
            continue
        name = f"fb_{cp:04X}"
        if name in out_order:
            continue
        fb_name = fb_cmap[cp]

        rec = DecomposingRecordingPen(fb_glyphs)
        fb_glyphs[fb_name].draw(TransformPen(rec, (scale, 0, 0, scale, 0, 0)))
        ttpen = TTGlyphPen(None)
        rec.replay(ttpen)
        glyph = ttpen.glyph()
        glyph.recalcBounds(out_glyf)

        out_glyf.glyphs[name] = glyph
        advance = int(round(fb["hmtx"][fb_name][0] * scale))
        out_hmtx[name] = (advance, int(glyph.xMin or 0))
        out_order.append(name)
        for table in out_font["cmap"].tables:
            if table.isUnicode():
                table.cmap[cp] = name
        added.append(cp)

    out_font.setGlyphOrder(out_order)
    out_font["maxp"].numGlyphs = len(out_order)
    log(f"Latin fallback '{fallback_path.name}': added {len(added)} missing glyph(s).")
    return added


def generate_option1(font_path_str, text_folder_str, output_folder_str, start_pua_str, log, fallback_path_str=""):
    font_path = Path(font_path_str)
    text_folder = Path(text_folder_str)
    output_folder = Path(output_folder_str)

    if not font_path.exists():
        raise FileNotFoundError(f"Noto Sans Thai font not found:\n{font_path}")
    if not text_folder.exists():
        raise FileNotFoundError(f"Text folder not found:\n{text_folder}")

    start_pua = parse_codepoint(start_pua_str)
    validate_unicode_codepoint(start_pua)

    files = collect_txt(text_folder)
    if not files:
        raise RuntimeError("No .txt files were found in the selected folder.")

    source_font = TTFont(str(font_path))
    if "glyf" not in source_font:
        raise RuntimeError(
            "Option 1 requires a TrueType font with a 'glyf' table. "
            "Use a TTF Noto Sans Thai font instead of a CFF-only OTF."
        )
    if "hmtx" not in source_font or "cmap" not in source_font:
        raise RuntimeError("The selected font is missing required font tables.")

    clusters = []
    seen = set()

    log(f"Scanning {len(files)} text file(s)...")
    for path in files:
        text = read_text(path)
        for cluster in thai_clusters(text):
            if cluster not in seen:
                seen.add(cluster)
                clusters.append(cluster)

    if not clusters:
        raise RuntimeError("No Thai clusters were found.")

    available = 0x10FFFF - start_pua + 1
    if len(clusters) > available:
        raise RuntimeError(
            f"Not enough Unicode codepoints from the selected start. Need {len(clusters)}."
        )

    log(f"Unique Thai clusters: {len(clusters)}")

    # Work on a new font object loaded from the original file.
    out_font = TTFont(str(font_path))
    out_glyf = out_font["glyf"]
    out_hmtx = out_font["hmtx"]
    src_glyph_set = source_font.getGlyphSet()
    out_order = list(out_font.getGlyphOrder())

    thai_to_pua = {}
    pua_to_thai = {}
    next_pua = start_pua

    for index, cluster in enumerate(clusters, 1):
        cp = next_pua
        glyph_name = f"pua_{cp:04X}"
        while glyph_name in out_order:
            next_pua += 1
            while 0xD800 <= next_pua <= 0xDFFF:
                next_pua += 1
            if next_pua > 0x10FFFF:
                raise RuntimeError("Unicode codepoint range exhausted.")
            cp = next_pua
            glyph_name = f"pua_{cp:04X}"

        _, shaped = shape_text(font_path, cluster)

        # Draw the cluster exactly where the font places it: no shifting, no centring.
        # The slot width is the font's own advance (HarfBuzz positions), so spacing
        # and size come from the font, not from the shape of the glyph ink.
        # Box width follows the letter only: mark glyphs (GDEF class 3, e.g. vowels
        # above/below and tone marks) do not add width, so they overflow the box.
        # The letter (consonant, plus SARA AM) is centred inside that box; vowels/marks move with it.
        box_width = letter_advance(source_font, shaped)
        ink_center = letter_ink_center(source_font, shaped)
        dx = (box_width / 2 - ink_center) if ink_center is not None else 0
        glyph, total_advance = build_cluster_glyph(src_glyph_set, source_font, shaped, dx)
        log(f"  centre: box={box_width} ink_center={ink_center} dx={dx}")
        glyph.recalcBounds(out_glyf)
        out_glyf[glyph_name] = glyph

        width = int(round(box_width))
        if width <= 0:
            for item in shaped:
                source_name = gid_to_name(source_font, item["gid"])
                if source_name and source_name in source_font["hmtx"].metrics:
                    width = int(source_font["hmtx"][source_name][0])
                    break

        out_hmtx[glyph_name] = (max(0, width), 0)
        out_order.append(glyph_name)

        thai_to_pua[cluster] = codepoint_name(cp)
        pua_to_thai[codepoint_name(cp)] = cluster

        log(f"[{index}/{len(clusters)}] {cluster!r} -> {codepoint_name(cp)}")

        next_pua += 1
        while 0xD800 <= next_pua <= 0xDFFF:
            next_pua += 1
        if next_pua > 0x10FFFF and index != len(clusters):
            raise RuntimeError("Unicode codepoint range exhausted.")

    out_font.setGlyphOrder(out_order)

    # Add PUA codepoints to every Unicode cmap subtable.
    for table in out_font["cmap"].tables:
        if table.isUnicode():
            for cluster, pua in thai_to_pua.items():
                cp = parse_codepoint(pua)
                table.cmap[cp] = f"pua_{cp:04X}"

    if fallback_path_str and fallback_path_str.strip():
        fb_path = Path(fallback_path_str.strip())
        if not fb_path.exists():
            raise FileNotFoundError(f"Latin fallback font not found:\n{fb_path}")
        try:
            add_missing_latin(out_font, out_order, fb_path, log)
        except Exception:
            log("WARNING: Latin fallback failed; the font was generated WITHOUT English glyphs.")
            log(traceback.format_exc())

    out_font["maxp"].numGlyphs = len(out_font.getGlyphOrder())

    output_folder.mkdir(parents=True, exist_ok=True)
    converted_folder = output_folder / "converted"
    converted_folder.mkdir(parents=True, exist_ok=True)

    font_out = output_folder / "Thai_PUA.ttf"
    map_out = output_folder / "mapping.json"

    out_font.save(str(font_out))

    map_data = {
        "version": 1,
        "type": "thai_unicode_slot",
        "start_codepoint": codepoint_name(start_pua),
        "start_pua": codepoint_name(start_pua),
        "font": font_path.name,
        "thai_to_pua": thai_to_pua,
        "pua_to_thai": pua_to_thai,
    }
    map_out.write_text(
        json.dumps(map_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Plain list: each converted Thai cluster and the PUA character itself, one per line.
    list_out = output_folder / "thai_pua_list.txt"
    list_lines = [f'"{thai}": "{chr(parse_codepoint(pua))}",' for thai, pua in thai_to_pua.items()]
    write_text(list_out, "\n".join(list_lines) + "\n")

    # Second list: every PUA character on ONE line, separated by a space.
    chars_out = output_folder / "thai_pua_chars.txt"
    write_text(chars_out, " ".join(chr(parse_codepoint(pua)) for pua in thai_to_pua.values()) + "\n")

    ordered = sorted(thai_to_pua.items(), key=lambda x: len(x[0]), reverse=True)
    for path in files:
        text = read_text(path)
        for cluster, pua in ordered:
            text = text.replace(cluster, chr(parse_codepoint(pua)))

        relative = path.relative_to(text_folder)
        write_text(converted_folder / relative, text)

    log("")
    log("OPTION 1 COMPLETE")
    log(f"Font:      {font_out}")
    log(f"Mapping:   {map_out}")
    log(f"List:      {list_out}")
    log(f"Chars:     {chars_out}")
    log(f"Converted: {converted_folder}")

    return font_out, map_out, converted_folder


# ============================================================
# OPTION 2
# ============================================================

def load_mapping(mapping_path: Path):
    if not mapping_path.exists():
        raise FileNotFoundError(f"mapping.json not found:\n{mapping_path}")

    data = json.loads(mapping_path.read_text(encoding="utf-8"))
    if data.get("type") not in {"thai_pua", "thai_unicode_slot", "thai_to_unicode_slot_converter"}:
        raise ValueError("This mapping.json is not a supported Thai mapping.")
    return data


def usable_mapping_items(table):
    """Only entries that are current conversion clusters. Older mapping.json files may
    contain plain consonants (e.g. น); those must stay as normal Thai text."""
    return [(k, v) for k, v in table.items() if THAI_CLUSTER_RE.fullmatch(k)]


def convert_thai_to_pua(text, mapping):
    table = mapping.get("thai_to_pua", {})
    for thai, pua in sorted(usable_mapping_items(table), key=lambda x: len(x[0]), reverse=True):
        text = text.replace(thai, chr(parse_codepoint(pua)))
    return text


def convert_pua_to_thai(text, mapping):
    table = mapping.get("pua_to_thai", {})
    for pua, thai in table.items():
        text = text.replace(chr(parse_codepoint(pua)), thai)
    return text


def generate_option2(mapping_str, input_str, output_str, direction, log):
    mapping_path = Path(mapping_str)
    input_folder = Path(input_str)
    output_folder = Path(output_str)

    mapping = load_mapping(mapping_path)

    if not input_folder.exists():
        raise FileNotFoundError(f"Input folder not found:\n{input_folder}")

    files = collect_txt(input_folder)
    if not files:
        raise RuntimeError("No .txt files were found in the input folder.")

    log(f"Files: {len(files)}")
    log(f"Direction: {direction}")

    for path in files:
        text = read_text(path)
        if direction == "thai_to_pua":
            converted = convert_thai_to_pua(text, mapping)
        else:
            converted = convert_pua_to_thai(text, mapping)

        relative = path.relative_to(input_folder)
        write_text(output_folder / relative, converted)
        log(f"Converted: {relative}")

    log("")
    log("OPTION 2 COMPLETE")
    log(f"Output: {output_folder}")
    return output_folder


# ============================================================
# OPTION 3
# Simple Thai -> PUA converter with text boxes + mapping.json
# ============================================================

def save_mapping_file(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def option3_convert_text(text, mapping):
    table = mapping.get("thai_to_pua", {})
    for thai, pua in sorted(usable_mapping_items(table), key=lambda x: len(x[0]), reverse=True):
        text = text.replace(thai, chr(parse_codepoint(pua)))
    return text


def option3_create_mapping_from_text(text, start_codepoint, log):
    """Create/reuse a Thai cluster -> arbitrary Unicode codepoint mapping."""
    start_cp = parse_codepoint(start_codepoint)
    validate_unicode_codepoint(start_cp)

    clusters = []
    seen = set()
    for cluster in thai_clusters(text):
        if cluster not in seen:
            seen.add(cluster)
            clusters.append(cluster)

    mapping = {}
    reverse = {}
    cp = start_cp
    for cluster in clusters:
        while cp in reverse and cp <= 0x10FFFF:
            cp += 1
        validate_unicode_codepoint(cp)
        mapping[cluster] = codepoint_name(cp)
        reverse[codepoint_name(cp)] = cluster
        log(f"{cluster!r} -> {codepoint_name(cp)}")
        cp += 1

    return {
        "version": 1,
        "type": "thai_to_unicode_slot_converter",
        "thai_to_pua": mapping,
        "pua_to_thai": reverse,
    }


def generate_option3_mapping(mapping_path_str, text, start_codepoint_str, log):
    """Option 3: only Thai text -> Unicode/PUA text box conversion."""
    mapping_path = Path(mapping_path_str) if mapping_path_str.strip() else None

    if mapping_path:
        if not mapping_path.exists():
            raise FileNotFoundError(f"mapping.json not found:\n{mapping_path}")
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        if "thai_to_pua" not in mapping:
            raise ValueError("mapping.json must contain 'thai_to_pua'.")
        converted = option3_convert_text(text, mapping)
        return converted, mapping

    mapping = option3_create_mapping_from_text(text, start_codepoint_str, log)
    converted = option3_convert_text(text, mapping)
    return converted, mapping


# ============================================================
# GUI
# ============================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title(APP_NAME)
        self.geometry("1120x850")
        self.minsize(950, 700)
        self.configure(bg="#111827")

        self.style = ttk.Style(self)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass

        self.style.configure("TFrame", background="#111827")
        self.style.configure(
            "TLabel", background="#111827", foreground="#E5E7EB",
            font=("Segoe UI", 10)
        )
        self.style.configure(
            "TLabelframe", background="#111827", foreground="#F9FAFB"
        )
        self.style.configure(
            "TLabelframe.Label", background="#111827", foreground="#F9FAFB",
            font=("Segoe UI", 10, "bold")
        )
        self.style.configure(
            "TNotebook", background="#111827", borderwidth=0
        )
        self.style.configure(
            "TNotebook.Tab", padding=(18, 10),
            font=("Segoe UI", 10, "bold")
        )
        self.style.configure("TButton", padding=(12, 8), font=("Segoe UI", 10, "bold"))
        self.style.configure("TEntry", padding=7)
        self.style.configure("TCombobox", padding=6)

        self._create_variables()
        self._build_ui()

        self.log("Thai PUA Tool ready.")
        self.log("Option 1 = generate Thai Unicode/PUA font slots.")
        self.log("Option 2 = convert folders using mapping.json.")
        self.log("Option 3 = simple Thai -> Unicode/PUA text converter.")

    # --------------------------------------------------------
    # Variables
    # --------------------------------------------------------
    def _create_variables(self):
        thai = str(find_noto_thai() or "")

        self.o1_font = tk.StringVar(value=thai)
        self.o1_text_folder = tk.StringVar()
        self.o1_output = tk.StringVar()
        self.o1_start = tk.StringVar(value="E000")
        self.o1_preset = tk.StringVar(value="PUA (Private Use Area)")
        fb = find_fallback_latin()
        self.o1_fallback = tk.StringVar(value=str(fb) if fb else "")

        self.o2_mapping = tk.StringVar()
        self.o2_input = tk.StringVar()
        self.o2_output = tk.StringVar()
        self.o2_direction = tk.StringVar(value="thai_to_pua")

        self.o3_mapping = tk.StringVar()
        self.o3_start = tk.StringVar(value="E000")
        self.o3_input = tk.StringVar()

    # --------------------------------------------------------
    # UI helpers
    # --------------------------------------------------------
    def make_field(self, parent, row, label, variable, command, button="Browse"):
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", padx=10, pady=8
        )
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", padx=10, pady=8)
        ttk.Button(parent, text=button, command=command).grid(
            row=row, column=2, padx=10, pady=8
        )
        return entry

    def make_tab(self, notebook, title):
        frame = ttk.Frame(notebook, padding=18)
        notebook.add(frame, text=title)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        return frame

    def _build_ui(self):
        header = tk.Frame(self, bg="#111827")
        header.pack(fill="x", padx=22, pady=(18, 8))

        tk.Label(
            header, text="THAI PUA TOOL", bg="#111827", fg="#F9FAFB",
            font=("Segoe UI", 22, "bold")
        ).pack(anchor="w")
        tk.Label(
            header,
            text="Thai PUA Font • Bidirectional Converter • Simple Manual PUA Converter. tool by Borin",
            bg="#111827", fg="#9CA3AF", font=("Segoe UI", 10)
        ).pack(anchor="w", pady=(3, 0))

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=16, pady=8)

        self.tab1 = self.make_tab(notebook, "Option 1 — Thai PUA Font")
        self.tab2 = self.make_tab(notebook, "Option 2 — PUA Converter")
        self.tab3 = self.make_tab(notebook, "Option 3 — Thai → PUA")

        self._build_option1()
        self._build_option2()
        self._build_option3()

        log_frame = ttk.LabelFrame(self, text="Log", padding=8)
        log_frame.pack(fill="both", expand=False, padx=16, pady=(0, 16))

        self.log_text = tk.Text(
            log_frame, height=9, wrap="word", bg="#0B1220", fg="#D1D5DB",
            insertbackground="#FFFFFF", relief="flat", font=("Consolas", 9)
        )
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scroll.set)

    # --------------------------------------------------------
    # Option 1
    # --------------------------------------------------------
    def _build_option1(self):
        frame = ttk.LabelFrame(self.tab1, text="Option 1 — Generate Thai Font Slots", padding=14)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)

        self.make_field(frame, 0, "Noto Sans Thai Font", self.o1_font, self.browse_o1_font)
        self.make_field(frame, 1, "Text Folder", self.o1_text_folder, self.browse_o1_text)
        self.make_field(frame, 2, "Output Folder", self.o1_output, self.browse_o1_output)

        ttk.Label(frame, text="Codepoint Preset").grid(
            row=3, column=0, sticky="w", padx=10, pady=8
        )
        presets = [
            "PUA (Private Use Area)",
            "Latin Extended", "Thai", "Khmer", "Hiragana", "Katakana",
            "Chinese / CJK", "Arabic", "Cyrillic", "Greek", "Custom"
        ]
        combo = ttk.Combobox(
            frame, textvariable=self.o1_preset, values=presets,
            state="readonly", width=28
        )
        combo.grid(row=3, column=1, sticky="w", padx=10, pady=8)
        combo.bind("<<ComboboxSelected>>", self.on_o1_preset)

        ttk.Label(frame, text="Start Codepoint").grid(
            row=4, column=0, sticky="w", padx=10, pady=8
        )
        ttk.Entry(frame, textvariable=self.o1_start, width=24).grid(
            row=4, column=1, sticky="w", padx=10, pady=8
        )

        ttk.Label(
            frame,
            text=(
                "Start Codepoint can be ANY Unicode codepoint, including PUA, "
                "Chinese, Thai, Latin, Khmer, or another Unicode block. "
                "Example: E000, 4E00, 0E01, 1780."
            ),
            wraplength=820, justify="left"
        ).grid(row=5, column=0, columnspan=3, sticky="w", padx=10, pady=(8, 18))

        self.make_field(frame, 6, "Latin Fallback Font (optional)", self.o1_fallback, self.browse_o1_fallback)

        ttk.Button(
            frame, text="GENERATE FONT", command=self.run_o1
        ).grid(row=7, column=0, columnspan=3, sticky="ew", padx=10, pady=10)

    def on_o1_preset(self, _event=None):
        starts = {
            "PUA (Private Use Area)": "E000",
            "Latin Extended": "0100",
            "Thai": "0E00",
            "Khmer": "1780",
            "Hiragana": "3040",
            "Katakana": "30A0",
            "Chinese / CJK": "4E00",
            "Arabic": "0600",
            "Cyrillic": "0400",
            "Greek": "0370",
        }
        value = starts.get(self.o1_preset.get())
        if value:
            self.o1_start.set(value)

    # --------------------------------------------------------
    # Option 2
    # --------------------------------------------------------
    def _build_option2(self):
        frame = ttk.LabelFrame(self.tab2, text="Option 2 — Thai ↔ PUA Folder Converter", padding=14)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)

        self.make_field(frame, 0, "mapping.json", self.o2_mapping, self.browse_o2_mapping, "Select")
        self.make_field(frame, 1, "Input Folder", self.o2_input, self.browse_o2_input)
        self.make_field(frame, 2, "Output Folder", self.o2_output, self.browse_o2_output)

        ttk.Label(frame, text="Direction").grid(
            row=3, column=0, sticky="w", padx=10, pady=8
        )
        box = ttk.Frame(frame)
        box.grid(row=3, column=1, columnspan=2, sticky="w", padx=10, pady=8)
        ttk.Radiobutton(
            box, text="Thai → PUA", variable=self.o2_direction,
            value="thai_to_pua"
        ).pack(side="left", padx=(0, 24))
        ttk.Radiobutton(
            box, text="PUA → Thai", variable=self.o2_direction,
            value="pua_to_thai"
        ).pack(side="left")

        ttk.Label(
            frame,
            text="Uses mapping.json. Processes all .txt files recursively and preserves subfolders.",
            wraplength=820, justify="left"
        ).grid(row=4, column=0, columnspan=3, sticky="w", padx=10, pady=(12, 18))

        ttk.Button(frame, text="CONVERT", command=self.run_o2).grid(
            row=5, column=0, columnspan=3, sticky="ew", padx=10, pady=10
        )

    # --------------------------------------------------------
    # Option 3 — ONLY text boxes + mapping.json
    # --------------------------------------------------------
    def _build_option3(self):
        outer = ttk.LabelFrame(self.tab3, text="Option 3 — Manual Thai → PUA", padding=14)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        self.make_field(
            outer, 0, "mapping.json", self.o3_mapping,
            self.browse_o3_mapping, "Select"
        )

        top = ttk.Frame(outer)
        top.grid(row=1, column=0, sticky="ew", padx=10, pady=(4, 10))
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="Start Codepoint").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        ttk.Entry(top, textvariable=self.o3_start, width=18).grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(
            top,
            text="Used only when mapping.json is not selected.",
        ).grid(row=0, column=2, sticky="w", padx=12)

        editor = ttk.Frame(outer)
        editor.grid(row=2, column=0, sticky="nsew", padx=10, pady=8)
        editor.columnconfigure(0, weight=1)
        editor.columnconfigure(1, weight=1)
        editor.rowconfigure(1, weight=1)

        ttk.Label(editor, text="Thai Input").grid(
            row=0, column=0, sticky="w", pady=(0, 5)
        )
        ttk.Label(editor, text="PUA / Unicode Output").grid(
            row=0, column=1, sticky="w", pady=(0, 5)
        )

        self.o3_input_text = tk.Text(
            editor, wrap="word", undo=True, bg="#0B1220", fg="#F9FAFB",
            insertbackground="#FFFFFF", relief="flat", font=("Noto Sans Thai", 16)
        )
        self.o3_input_text.grid(row=1, column=0, sticky="nsew", padx=(0, 6))

        self.o3_output_text = tk.Text(
            editor, wrap="word", undo=True, bg="#0B1220", fg="#F9FAFB",
            insertbackground="#FFFFFF", relief="flat", font=("Segoe UI", 16)
        )
        self.o3_output_text.grid(row=1, column=1, sticky="nsew", padx=(6, 0))

        buttons = ttk.Frame(outer)
        buttons.grid(row=3, column=0, sticky="ew", padx=10, pady=(10, 4))
        for i in range(5):
            buttons.columnconfigure(i, weight=1)

        ttk.Button(buttons, text="CONVERT", command=self.run_o3).grid(
            row=0, column=0, sticky="ew", padx=4
        )
        ttk.Button(buttons, text="COPY OUTPUT", command=self.copy_o3_output).grid(
            row=0, column=1, sticky="ew", padx=4
        )
        ttk.Button(buttons, text="CLEAR", command=self.clear_o3).grid(
            row=0, column=2, sticky="ew", padx=4
        )
        ttk.Button(buttons, text="SAVE MAPPING", command=self.save_o3_mapping).grid(
            row=0, column=3, sticky="ew", padx=4
        )
        ttk.Button(buttons, text="LOAD MAPPING", command=self.load_o3_mapping).grid(
            row=0, column=4, sticky="ew", padx=4
        )

        ttk.Label(
            outer,
            text=(
                "Option 3 only does Thai → PUA/Unicode conversion. "
                "Type Thai manually in the left box. If mapping.json is loaded, "
                "that mapping is used. Otherwise the tool creates a mapping from "
                "the unique Thai syllable clusters in the box, starting at Start Codepoint."
            ),
            wraplength=900, justify="left"
        ).grid(row=4, column=0, sticky="w", padx=10, pady=(10, 4))

    # --------------------------------------------------------
    # Browse
    # --------------------------------------------------------
    def browse_font(self, variable):
        path = filedialog.askopenfilename(
            title="Select Font",
            filetypes=[("Font files", "*.ttf *.otf *.ttc"), ("All files", "*.*")]
        )
        if path:
            variable.set(path)

    def browse_folder(self, variable):
        path = filedialog.askdirectory(title="Select Folder")
        if path:
            variable.set(path)

    def browse_o1_font(self):
        self.browse_font(self.o1_font)
    def browse_o1_text(self):
        self.browse_folder(self.o1_text_folder)
    def browse_o1_output(self):
        self.browse_folder(self.o1_output)
    def browse_o1_fallback(self):
        self.browse_font(self.o1_fallback)

    def browse_o2_mapping(self):
        path = filedialog.askopenfilename(
            title="Select mapping.json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        if path:
            self.o2_mapping.set(path)

    def browse_o2_input(self):
        self.browse_folder(self.o2_input)
    def browse_o2_output(self):
        self.browse_folder(self.o2_output)

    def browse_o3_mapping(self):
        path = filedialog.askopenfilename(
            title="Select mapping.json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        if path:
            self.o3_mapping.set(path)
            self.load_o3_mapping(path)

    # --------------------------------------------------------
    # Log
    # --------------------------------------------------------
    def log(self, text):
        if not hasattr(self, "log_text"):
            return
        self.log_text.insert("end", str(text) + "\n")
        self.log_text.see("end")
        self.update_idletasks()

    def clear_log(self):
        if hasattr(self, "log_text"):
            self.log_text.delete("1.0", "end")

    # --------------------------------------------------------
    # Run Option 1
    # --------------------------------------------------------
    def run_o1(self):
        self.clear_log()
        self.log("=== OPTION 1 ===")
        try:
            if not self.o1_output.get().strip():
                self.o1_output.set(str(Path.cwd() / "output_option1"))

            font_out, map_out, converted = generate_option1(
                self.o1_font.get().strip(),
                self.o1_text_folder.get().strip(),
                self.o1_output.get().strip(),
                self.o1_start.get().strip(),
                self.log,
                fallback_path_str=self.o1_fallback.get().strip(),
            )

            messagebox.showinfo(
                "Option 1 Complete",
                f"Font generated successfully.\n\nFont:\n{font_out}\n\n"
                f"Mapping:\n{map_out}\n\nConverted:\n{converted}"
            )
        except Exception as exc:
            self.show_error("Option 1", exc)

    # --------------------------------------------------------
    # Run Option 2
    # --------------------------------------------------------
    def run_o2(self):
        self.clear_log()
        self.log("=== OPTION 2 ===")
        try:
            if not self.o2_output.get().strip():
                self.o2_output.set(str(Path.cwd() / "output_option2"))

            output = generate_option2(
                self.o2_mapping.get().strip(),
                self.o2_input.get().strip(),
                self.o2_output.get().strip(),
                self.o2_direction.get(),
                self.log,
            )

            messagebox.showinfo("Option 2 Complete", f"Conversion complete.\n\nOutput:\n{output}")
        except Exception as exc:
            self.show_error("Option 2", exc)

    # --------------------------------------------------------
    # Run Option 3
    # --------------------------------------------------------
    def run_o3(self):
        self.clear_log()
        self.log("=== OPTION 3 ===")
        try:
            text = self.o3_input_text.get("1.0", "end-1c")
            if not text:
                raise ValueError("Please type Thai text in the left box.")

            converted, mapping = generate_option3_mapping(
                self.o3_mapping.get().strip(),
                text,
                self.o3_start.get().strip(),
                self.log,
            )

            self.o3_output_text.delete("1.0", "end")
            self.o3_output_text.insert("1.0", converted)

            if self.o3_mapping.get().strip():
                self.log(f"Loaded mapping: {self.o3_mapping.get().strip()}")
            else:
                self.log("Mapping created in memory. Click SAVE MAPPING to save it.")

            self.log("OPTION 3 COMPLETE")
            messagebox.showinfo("Option 3 Complete", "Thai → PUA/Unicode conversion complete.")
        except Exception as exc:
            self.show_error("Option 3", exc)

    def copy_o3_output(self):
        text = self.o3_output_text.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        self.log("Option 3 output copied to clipboard.")

    def clear_o3(self):
        self.o3_input_text.delete("1.0", "end")
        self.o3_output_text.delete("1.0", "end")

    def save_o3_mapping(self):
        text = self.o3_input_text.get("1.0", "end-1c")
        if not text:
            messagebox.showwarning("Save Mapping", "Please type Thai text first.")
            return

        try:
            mapping_path = self.o3_mapping.get().strip()
            if mapping_path:
                path = Path(mapping_path)
            else:
                chosen = filedialog.asksaveasfilename(
                    title="Save mapping.json",
                    defaultextension=".json",
                    filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
                )
                if not chosen:
                    return
                path = Path(chosen)
                self.o3_mapping.set(str(path))

            _, mapping = generate_option3_mapping(
                "", text, self.o3_start.get().strip(), self.log
            )
            save_mapping_file(path, mapping)
            self.log(f"Mapping saved: {path}")
            messagebox.showinfo("Mapping Saved", f"Saved:\n{path}")
        except Exception as exc:
            self.show_error("Save Mapping", exc)

    def load_o3_mapping(self, path=None):
        try:
            if path is None:
                path = self.o3_mapping.get().strip()
                if not path:
                    chosen = filedialog.askopenfilename(
                        title="Load mapping.json",
                        filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
                    )
                    if not chosen:
                        return
                    path = chosen
                    self.o3_mapping.set(path)

            data = load_mapping(Path(path))
            self.log(f"Loaded mapping: {path}")
            self.log(f"Mappings: {len(data.get('thai_to_pua', {}))}")
            messagebox.showinfo(
                "Mapping Loaded",
                f"Loaded mapping.json successfully.\n\nMappings: {len(data.get('thai_to_pua', {}))}"
            )
        except Exception as exc:
            self.show_error("Load Mapping", exc)

    def show_error(self, option_name, exc):
        self.log("")
        self.log("ERROR")
        self.log(f"{type(exc).__name__}: {exc}")
        self.log(traceback.format_exc())
        messagebox.showerror(
            f"{option_name} Error",
            f"{type(exc).__name__}: {exc}\n\nSee the Log panel for the full traceback."
        )


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
