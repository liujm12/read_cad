"""Full pipeline: load all DWGs → unified axis grid → categorize → spatial match → master table."""
import csv, re, math
import sys, io
from pathlib import Path
from collections import defaultdict, Counter
from ezdxf.addons import odafc

if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass

CAD_DIR = Path(__file__).parent / "cad"
OUT_DIR = Path(__file__).parent / "result" / "all"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ═══════════════════════════════════
# PHASE 1: LOAD ALL FILES
# ═══════════════════════════════════

def load_all_dwgs(cad_dir):
    """Load all DWG files, return list of (filename, doc, modelspace_entities, floor)."""
    all_data = []
    for dwg_path in sorted(cad_dir.glob("*.dwg")):
        print(f"  Loading: {dwg_path.name}...")
        doc = odafc.readfile(str(dwg_path))
        msp_entities = list(doc.modelspace())
        # Extract floor from filename: e.g. "...-F3-..." → "F3"
        floor = None
        m = re.search(r'(?:[_-])?(F\d+|B\d+[A-Z]?|PB\d+[A-Z]?)', dwg_path.name, re.IGNORECASE)
        if m:
            floor = m.group(1).upper()
        all_data.append({
            "filename": dwg_path.name,
            "stem": dwg_path.stem,
            "doc": doc,
            "entities": msp_entities,
            "floor": floor,
        })
        floor_str = floor or "?"
        print(f"    {len(msp_entities)} entities, {len(doc.layers)} layers, floor={floor_str}")
    return all_data


def resolve_unknown_floors(all_data):
    """If any files have unknown floor, ask user to specify.

    Modifies all_data in-place. Returns True if all floors resolved.
    """
    unknown = [(i, d) for i, d in enumerate(all_data) if d["floor"] is None]
    if not unknown:
        return True

    known_floors = sorted(set(d["floor"] for d in all_data if d["floor"]))
    print("\n" + "=" * 70)
    print("⚠ 以下图纸无法自动识别楼层:")
    for idx, (i, d) in enumerate(unknown):
        print(f"  [{idx+1}] {d['filename']}")
    print()
    if known_floors:
        print(f"  已知楼层: {', '.join(known_floors)}")
    print("  请逐张指定楼层（如: 1=F2, 2=F3），输入 0=独立楼层")
    response = input("  > ").strip()

    # Parse user response
    for part in response.split(","):
        part = part.strip()
        m = re.match(r'(\d+)\s*=\s*(\S+)', part)
        if m:
            n = int(m.group(1)) - 1
            floor_val = m.group(2).upper()
            if 0 <= n < len(unknown):
                all_data[unknown[n][0]]["floor"] = floor_val if floor_val != "0" else f"_UNK{unknown[n][0]}"

    # Check if any still unresolved
    still_unknown = [d for d in all_data if d["floor"] is None]
    if still_unknown:
        # Assign auto floor names for remaining
        for i, d in enumerate(still_unknown):
            d["floor"] = f"_UNK{i+1}"
        print(f"  {len(still_unknown)} 张图纸仍未指定，已分配独立楼层\n")

    return True


# ═══════════════════════════════════
# PHASE 2: EXTRACT AXIS GRID
# ═══════════════════════════════════

def extract_axis_grid(all_data):
    """Find AR file with S-GRID data and extract axis positions.

    Returns: {
        'letter_axes': [(y_pos, letter), ...],   # sorted by Y
        'number_axes': [(x_pos, number), ...],   # sorted by X
        'letter_positions': {letter: y_pos},
        'number_positions': {number: x_pos},
    }
    """
    # Find the best AR file with grid
    grid_source = None
    for data in all_data:
        if "AR-F3-LAY-01" in data["filename"]:
            grid_source = data
            break
    if not grid_source:
        for data in all_data:
            if "AR" in data["filename"]:
                grid_source = data
                break
    if not grid_source:
        print("  WARNING: No AR file with grid found!")
        return None

    print(f"  Axis grid from: {grid_source['filename']}")

    # Collect letter axis labels (horizontal grid, Y-axis)
    letter_axes = {}
    for e in grid_source["entities"]:
        if e.dxf.layer != "S-GRID-IDEN":
            continue
        txt = ""
        if e.dxftype() == "MTEXT":
            try: txt = e.plain_text().strip()
            except: pass
        elif e.dxftype() == "TEXT" and e.dxf.hasattr("text"):
            txt = e.dxf.text.strip()
        if len(txt) == 1 and txt.isalpha() and e.dxf.hasattr("insert"):
            y = round(e.dxf.insert.y, 0)
            letter_axes[y] = max(letter_axes.get(y, ""), txt)  # prefer longer

    # Collect number axis labels (vertical grid, X-axis)
    number_axes = {}
    for e in grid_source["entities"]:
        if e.dxf.layer != "S-GRID-IDEN":
            continue
        txt = ""
        if e.dxftype() == "MTEXT":
            try: txt = e.plain_text().strip()
            except: pass
        elif e.dxftype() == "TEXT" and e.dxf.hasattr("text"):
            txt = e.dxf.text.strip()
        if txt.isdigit() and e.dxf.hasattr("insert"):
            x = round(e.dxf.insert.x, 0)
            number_axes[x] = max(number_axes.get(x, ""), txt, key=lambda n: int(n) if n.isdigit() else 0)

    # Sort
    letters = sorted(letter_axes.items())  # (y, letter)
    numbers = sorted(number_axes.items())  # (x, number)

    print(f"    Letter axes: {len(letters)} ({letters[0][1]}~{letters[-1][1]})")
    print(f"    Number axes: {len(numbers)} ({numbers[0][1]}~{numbers[-1][1]})")

    return {
        "letter_axes": letters,
        "number_axes": numbers,
        "letter_positions": {l: y for y, l in letters},
        "number_positions": {n: x for x, n in numbers},
    }


def assign_axis(x, y, grid):
    """Determine axis label for a point (x, y).

    Returns (axis_label, zone) tuple.
    Example: ("G/H轴", "南中区")
    """
    if not grid:
        return "", ""

    # Find nearest letter axes (horizontal)
    letters = grid["letter_axes"]  # [(y, letter), ...]
    if y <= letters[0][0]:
        lo_letter = letters[0][1]
        hi_letter = letters[0][1]
    elif y >= letters[-1][0]:
        lo_letter = letters[-1][1]
        hi_letter = letters[-1][1]
    else:
        for i in range(len(letters) - 1):
            if letters[i][0] <= y <= letters[i+1][0]:
                lo_letter = letters[i][1]
                hi_letter = letters[i+1][1]
                break
        else:
            lo_letter = hi_letter = "?"

    if lo_letter == hi_letter:
        axis_label = f"{lo_letter}轴"
    else:
        axis_label = f"{lo_letter}/{hi_letter}轴"

    # Determine zone by letter range
    zone = ""
    letter_ord = ord(lo_letter) if lo_letter else 0
    if letter_ord >= ord('V'):  # V, W, X, Y
        zone = "北区"
    elif letter_ord >= ord('L'):  # L, M, N, P, Q, R, S, T, U
        zone = "北中区"
    elif letter_ord >= ord('G'):  # G, H, J, K
        zone = "南中区"
    else:  # A, B, C, D, E, F
        zone = "南区"

    return axis_label, zone


# ═══════════════════════════════════
# PHASE 3: CATEGORIZE & SPATIAL MATCH
# ═══════════════════════════════════

def categorize_block(name, layer=""):
    """Same as before — classify block INSERT."""
    if not name:
        return "未知", ""

    base = re.sub(r'-\d+-\d+F$', '', name)
    base = re.sub(r'-V\d+-\d+F$', '', base)

    m = re.match(r'^(.+)\$0\$(.+)$', base)
    xref_prefix = m.group(1) if m else ""
    if m:
        base = m.group(2)

    # True XREF
    if re.match(r'^[XD]-(HLP|HL|ST|AR|CR|SM).*(LAY|SEC|KPART|KEQP)-\d{2}-\d{3}$', xref_prefix):
        return "外部参照"
    if re.match(r'^D-HLP\d-ST-.*BEA-\d', xref_prefix):
        return "外部参照"
    if re.match(r'^X-Wall', xref_prefix):
        return "外部参照"
    if re.search(r'(KPART|KEQP)-\d{2}-\d{3}$', xref_prefix) and not base:
        return "外部参照"
    if re.match(r'^[XD]-(HLP|HL|ST|AR|CR|SM)', base):
        return "外部参照"

    # GUID → use layer
    if re.match(r'^A\$C[A-F0-9]{7,8}$', base):
        if "BOLT" in layer.upper():  return "紧固件/螺栓"
        if "ANGEL" in layer.upper() or "ANGLE" in layer.upper(): return "型钢"
        if "DCC" in layer.upper():   return "DCC设备"

    # Doors
    if re.search(r'门|卷帘|闸机|DOR|DD_|ZDM|MD-?\d|QM\d', base, re.IGNORECASE):
        if '卷帘' in base: return "卷帘门"
        if '闸机' in base: return "闸机"
        if '防火' in base:
            return "双扇防火门" if ('双扇' in base or '双开' in base) else "单扇防火门"
        if '检修' in base or 'MD' in base: return "检修门"
        if '铝' in base: return "铝合金门"
        return "门"
    if re.search(r'窗|Window', base, re.IGNORECASE): return "窗"

    # Structure
    if re.search(r'柱|COLS|RHS|1800X', base, re.IGNORECASE):
        if '砼' in base or '混凝' in base: return "混凝土柱"
        if '钢' in base: return "钢柱"
        return "柱"
    if re.search(r'梁|BEAM|BW\d', base, re.IGNORECASE):
        return "钢梁" if '钢' in base else "梁"
    if re.search(r'柱底|找平|垫板|底板|BASE.?PLATE', base, re.IGNORECASE): return "柱底板/垫板"
    if re.search(r'LC\d', base, re.IGNORECASE): return "幕墙竖梃/结构构件"

    # Steel
    if re.search(r'ANGLE|Angle|角钢|角铁|方钢|方管.*X\d', base): return "型钢"
    if re.match(r'^C\d{3}X\d{2,3}', base): return "型钢"

    # DCC (exclude tiny blocks < 500mm that are bolts/fasteners mislabeled)
    if re.search(r'DCC[-]?\d', base, re.IGNORECASE):
        if re.search(r'BOLT|ANCHOR|化学锚栓', layer, re.IGNORECASE):
            return "紧固件/螺栓"
        return "DCC设备"
    if re.search(r'DCC-Rack|RACK|rack', base, re.IGNORECASE): return "DCC支架"
    if re.search(r'DCC-SEC|DIV-DOOR', base, re.IGNORECASE):   return "DCC隔断/门"

    # Openings
    if re.search(r'洞口|DOR-?\d|OPEN|墙洞', base, re.IGNORECASE):
        if '楼板' in base: return "楼板洞口"
        if '墙' in base: return "墙洞口"
        return "洞口"

    # Fasteners
    if re.search(r'^M\d{1,2}$|^M\d{1,2}[Xx]|螺栓|BOLT|ANCHOR|化学锚栓|膨胀螺栓', base, re.IGNORECASE):
        return "紧固件/螺栓"
    if re.match(r'^H\d{2}_\d{2,3}$', base): return "紧固件/螺栓"

    # MEP
    if re.search(r'水管|管道|PIPE|风管|DUCT|弯头|阀门|VALVE|地漏|排水', base): return "管道/管件"
    if re.search(r'泵|PUMP|风机|FAN|空调|冷却|COOL|加热|HEAT|水箱|TANK', base): return "设备"
    if re.search(r'配电|开关|插座|灯具|灯|LIGHT|照明|电缆|CABLE|桥架|TRAY', base, re.IGNORECASE): return "电气"
    if re.search(r'灭火|消防|消火栓|喷淋|烟感|报警', base): return "消防"

    # Other components
    if re.search(r'扶栏|栏杆|railing', base, re.IGNORECASE): return "扶栏"
    if re.search(r'爬梯|LADDER|梯', base, re.IGNORECASE):
        if '楼梯' in base: return "楼梯"
        return "爬梯" if ('爬梯' in base or 'LADDER' in base.upper()) else "梯"
    if re.search(r'挡烟|烟', base):  return "挡烟垂壁"
    if re.search(r'饮水|水机|洗手|设备|EQP|equip|COIL|coil', base, re.IGNORECASE): return "设备"

    # Annotations
    if re.search(r'轴网|GRID', base, re.IGNORECASE):  return "轴网符号"
    if re.search(r'剖切|section', base, re.IGNORECASE): return "剖切符号"
    if re.search(r'轴号|标记', base):   return "符号标记"
    if re.match(r'^CG$|^GR$', base):    return "符号"
    if re.search(r'Cloud|云线|BG$', base): return "修订标记"
    if re.search(r'TYPE\s*\d', base, re.IGNORECASE): return "类型标记"

    return "其他"


def extract_size_and_model(spec_text):
    """Parse spec string into width, height, orientation, model, qty_mult.

    *N suffix on model (e.g. "DCC-C*2") is stripped from model and returned as qty_mult.
    """
    w = h = orient = model = ""
    qty_mult = 1
    m = re.search(r'(\d{3,4})\s*[xX×\*]\s*(\d{3,4})', spec_text)
    if m:
        w = f"{int(m.group(1))/1000:.2f}"
        h = f"{int(m.group(2))/1000:.2f}"
    if '左' in spec_text: orient = '左'
    elif '右' in spec_text: orient = '右'
    # DCC letter model with optional *N quantity suffix: "DCC-F", "DCC-E*2"
    m = re.search(r'(DCC-[A-Z])(?:\*(\d+))?', spec_text, re.IGNORECASE)
    if m:
        model = m.group(1)
        if m.group(2):
            qty_mult = int(m.group(2))
    elif re.search(r'DCC-?\d', spec_text):
        m2 = re.search(r'(DCC-?\d+)', spec_text)
        if m2:
            model = re.sub(r'[-–]\d+.*$', '', m2.group(1)).strip()
    return w, h, orient, model, qty_mult


def model_sort_key(model):
    order = {'DCC-F':0, 'DCC-E':1, 'DCC-D':2, 'DCC-C':3, 'DCC-B':4, 'DCC-A':5,
             'DCC-7':6, 'DCC-6':7, 'DCC-5':8, 'DCC-4':9, 'DCC-J':10}
    if model.startswith('DCC-') and model[4:].isdigit():
        return order.get(model, 50 + int(model[4:]))
    return order.get(model, 99)


def get_block_geometry(doc, block_name):
    """Get the bounding box dimensions (width, height) of a block definition in mm.

    Returns (width, height) or (0, 0) if not computable.
    Results are cached per (doc, block_name) for performance.
    """
    # Try to find the block definition
    block = doc.blocks.get(block_name)
    if block is None:
        # Try stripping XREF prefix: "XREF$0$BLOCK" → "BLOCK"
        simple = re.sub(r'^.*\$0\$', '', block_name)
        if simple != block_name:
            block = doc.blocks.get(simple)
    if block is None:
        return 0, 0

    # Compute bounding box from block's constituent entities
    min_x = min_y = float('inf')
    max_x = max_y = float('-inf')
    has_geometry = False

    for e in block:
        try:
            if e.dxftype() == 'LINE':
                sx, sy = e.dxf.start.x, e.dxf.start.y
                ex, ey = e.dxf.end.x, e.dxf.end.y
                min_x, max_x = min(min_x, sx, ex), max(max_x, sx, ex)
                min_y, max_y = min(min_y, sy, ey), max(max_y, sy, ey)
                has_geometry = True
            elif e.dxftype() == 'LWPOLYLINE':
                for px, py, *_ in e.get_points():
                    min_x, max_x = min(min_x, px), max(max_x, px)
                    min_y, max_y = min(min_y, py), max(max_y, py)
                    has_geometry = True
            elif e.dxftype() in ('CIRCLE', 'ARC'):
                cx, cy = e.dxf.center.x, e.dxf.center.y
                r = e.dxf.radius
                min_x, max_x = min(min_x, cx - r), max(max_x, cx + r)
                min_y, max_y = min(min_y, cy - r), max(max_y, cy + r)
                has_geometry = True
        except Exception:
            pass

    if not has_geometry:
        return 0, 0
    return round(max_x - min_x, 2), round(max_y - min_y, 2)


# ═══════════════════════════════════
# PHASE 4: BUILD TEXT INDEX (cross-file)
# ═══════════════════════════════════

def build_unified_text_index(all_data):
    """Build text spatial index from ALL files' DCC-Size and annotation layers."""
    text_index = []
    text_layers = {"DCC-Size", "K-KART-TEXT", "A-ANNO-NOTE", "G-ANNO-TEXT", "k-m2"}
    for data in all_data:
        for e in data["entities"]:
            if e.dxftype() in ("TEXT", "MTEXT") and e.dxf.layer in text_layers:
                if not e.dxf.hasattr("insert"):
                    continue
                x, y = e.dxf.insert.x, e.dxf.insert.y
                txt = ""
                if e.dxftype() == "TEXT" and e.dxf.hasattr("text"):
                    txt = e.dxf.text.strip()
                elif e.dxftype() == "MTEXT":
                    try: txt = e.plain_text().strip()
                    except: pass
                if txt:
                    text_index.append((x, y, txt, data["stem"]))
    return text_index


def find_nearby_labels(x, y, text_index, threshold=5000):
    """Find dimension and model labels near a point."""
    nearby = []
    for tx, ty, txt, src in text_index:
        d = math.sqrt((x - tx)**2 + (y - ty)**2)
        if d < threshold:
            nearby.append((d, txt))
    nearby.sort()

    dim_texts = []
    model_texts = []
    for d, txt in nearby:
        if re.search(r'\d{3,4}\s*[xX×\*]\s*\d{3,4}', txt):
            dim_texts.append(txt)
        # Match DCC-letter (DCC-F, DCC-E*2) AND DCC-number (DCC-4, DCC-7)
        elif re.match(r'^DCC-[A-Z0-9]', txt, re.IGNORECASE):
            # Clean up quantity notes: "DCC-4-1,8台" → "DCC-4"
            clean = re.sub(r'[-–]\d+.*$', '', txt).strip()
            if clean:
                model_texts.append(clean)

    label = dim_texts[0] if dim_texts else ""
    model = model_texts[0] if model_texts else ""
    return label, model


# ═══════════════════════════════════
# PHASE 5: MAIN PIPELINE
# ═══════════════════════════════════

def main():
    print("=" * 70)
    print("PHASE 1: Loading all DWG files")
    print("=" * 70)
    all_data = load_all_dwgs(CAD_DIR)
    print(f"  Total: {len(all_data)} files loaded")
    resolve_unknown_floors(all_data)
    print()

    print("=" * 70)
    print("PHASE 2: Extracting axis grid")
    print("=" * 70)
    grid = extract_axis_grid(all_data)
    print()

    print("=" * 70)
    print("PHASE 3: Building text index")
    print("=" * 70)
    text_index = build_unified_text_index(all_data)
    print(f"  {len(text_index)} text annotations indexed\n")

    print("=" * 70)
    print("PHASE 4: Collecting & classifying all INSERTs")
    print("=" * 70)

    # Collect all INSERT entities with coordinates
    all_inserts = []
    for data in all_data:
        source = data["stem"]
        floor = data["floor"]
        for e in data["entities"]:
            if e.dxftype() != "INSERT":
                continue
            if not e.dxf.hasattr("insert"):
                continue
            block_name = e.dxf.name if e.dxf.hasattr("name") else ""
            layer = e.dxf.layer
            x = e.dxf.insert.x
            y = e.dxf.insert.y

            # Skip block definition templates dumped at origin
            if abs(x) < 10 and abs(y) < 10:
                continue

            cat = categorize_block(block_name, layer)
            if cat in ("外部参照", "未知", "轴网符号", "剖切符号", "符号", "符号标记", "修订标记", "类型标记"):
                continue

            # Spatial matching for labels (cascade: 5m → 10m)
            label, model = find_nearby_labels(x, y, text_index, 5000)
            if not label:
                label, model = find_nearby_labels(x, y, text_index, 10000)

            # Combine spec — try to get raw dimensions from block name
            raw_size = ""
            # Priority 1: explicit NNNNxNNNN pattern (e.g. DCC-2400x1200, 2850x1800)
            m = re.search(r'(\d{3,4})[Xx×](\d{3,4})', block_name)
            if m:
                raw_size = f"{m.group(1)}x{m.group(2)}"
            # Priority 2: cm/decimal notation (e.g. DCC-28.5x26 → 2850x2600)
            if not raw_size:
                m = re.search(r'(\d{2}\.?\d?)[xX](\d{2})', block_name)
                if m:
                    raw_size = f"{int(float(m.group(1))*100)}x{int(m.group(2))*100}"
            # Priority 3: DCCnnnn → DCC3523 → 3500x2300
            if not raw_size:
                m = re.match(r'DCC(\d{2})(\d{2})', block_name)
                if m:
                    raw_size = f"{int(m.group(1))*100}x{int(m.group(2))*100}"
            # Priority 4: 4-digit suffix (e.g. FM1123 → 1100x2300)
            if not raw_size:
                m = re.search(r'(\d{2})(\d{2})$', block_name)
                if m and not re.search(r'[A-Za-z]', str(m.group(0))):
                    raw_size = f"{int(m.group(1))*100}x{int(m.group(2))*100}"

            if label and raw_size and raw_size in label:
                spec = label
            elif label:
                spec = (raw_size + " " + label).strip()
            else:
                spec = raw_size

            if model:
                spec = (spec + " " + model).strip()

            w, h, orient, parsed_model, qty_mult = extract_size_and_model(spec)
            if not parsed_model and model:
                # Strip *N suffix from raw model text: "DCC-C*2" → model="DCC-C", qty×2
                m = re.match(r'(.+?)\*(\d+)$', model)
                if m:
                    parsed_model = m.group(1)
                    qty_mult *= int(m.group(2))
                else:
                    parsed_model = model

            # Fallback: extract model from block name if spatial matching failed
            if not parsed_model:
                bn = block_name
                # Strip XREF prefix
                bn = re.sub(r'^.*\$0\$', '', bn)
                # DCC-1-3 → DCC-1, DCC-3-1 → DCC-3
                m = re.match(r'(DCC-\d+)', bn)
                if m:
                    parsed_model = m.group(1)
                # DCC-2400x1200 → DCC-2400
                elif re.match(r'DCC-\d{3,4}[xX]\d{3,4}', bn):
                    parsed_model = re.match(r'(DCC-\d{3,4}[xX]\d{3,4})', bn).group(1)
                # DCC-2406 → DCC-2406
                elif re.match(r'DCC-\d{4}$', bn):
                    parsed_model = re.match(r'(DCC-\d{4})', bn).group(1)
                # DCC2850x1800(右) → model = DCC-2850x1800
                elif re.match(r'DCC\d{3,4}[xX]\d{3,4}', bn):
                    m = re.match(r'(DCC\d{3,4}[xX]\d{3,4})', bn)
                    parsed_model = m.group(1).replace('x', 'x').replace('X', 'x')
                # DCC3523左接管 → model = DCC-3523
                elif re.match(r'DCC\d+', bn):
                    m = re.match(r'(DCC\d+)', bn)
                    parsed_model = m.group(1)

            # Re-classify: tiny items (both dims < 500mm) are fasteners, not DCC
            if cat in ("DCC设备", "DCC支架") and w and h:
                if float(w) < 0.5 and float(h) < 0.5:
                    cat = "紧固件/螺栓"
            # Block-name fallback for DCC支架 (same logic as DCC设备)
            if cat == "DCC支架" and not parsed_model:
                bn = re.sub(r'^.*\$0\$', '', block_name)
                m = re.match(r'(DCC-\d+)', bn)
                if m: parsed_model = m.group(1)
                elif re.match(r'DCC\d{3,4}[xX]\d{3,4}', bn):
                    m = re.match(r'(DCC\d{3,4}[xX]\d{3,4})', bn)
                    parsed_model = m.group(1)
                elif re.match(r'DCC\d+', bn):
                    m = re.match(r'(DCC\d+)', bn)
                    parsed_model = m.group(1)

            # Dimension-based quantity inference for DCC items:
            # compare label single-unit size vs actual block geometry
            if cat in ("DCC设备", "DCC支架") and w and h:
                sx = e.dxf.xscale if e.dxf.hasattr('xscale') else 1
                sy = e.dxf.yscale if e.dxf.hasattr('yscale') else 1
                block_w, block_h = get_block_geometry(data["doc"], block_name)
                if block_w > 0 and block_h > 0:
                    actual_w = block_w * abs(sx)
                    actual_h = block_h * abs(sy)
                    label_w_mm = float(w) * 1000
                    label_h_mm = float(h) * 1000
                    if label_w_mm > 0 and label_h_mm > 0:
                        ratio_w = actual_w / label_w_mm
                        ratio_h = actual_h / label_h_mm
                        n_w, n_h = round(ratio_w), round(ratio_h)
                        # 3% tolerance: integer ratio >= 2
                        if n_w >= 2 and abs(ratio_w - n_w) < 0.03:
                            qty_mult *= n_w
                        if n_h >= 2 and abs(ratio_h - n_h) < 0.03:
                            qty_mult *= n_h

            # Axis assignment
            axis_label, zone = assign_axis(x, y, grid)

            all_inserts.append({
                "source": source,
                "floor": floor,
                "cat": cat,
                "block_name": block_name,
                "spec": spec,
                "width": w,
                "height": h,
                "orient": orient,
                "model": parsed_model,
                "qty": qty_mult,
                "axis": axis_label,
                "zone": zone,
                "x": round(x, 2),
                "y": round(y, 2),
                "layer": layer,
            })

    print(f"  {len(all_inserts)} non-XREF INSERTs collected")

    # ── Post-processing: dimension-based model inference ──
    # For items without model, match by dimensions to known models
    known_models = defaultdict(Counter)  # (w, h, orient) -> {model: count}
    for it in all_inserts:
        if it["model"] and it["width"] and it["height"]:
            known_models[(it["width"], it["height"], it["orient"])][it["model"]] += 1
        elif it["model"] and it["width"] and it["height"] and not it["orient"]:
            known_models[(it["width"], it["height"], "")][it["model"]] += 1

    filled = 0
    for it in all_inserts:
        if it["cat"] not in ("DCC设备", "DCC支架") or it["model"]:
            continue
        w, h, orient = it["width"], it["height"], it["orient"]
        # Try exact match first, then without orientation
        for key in [(w, h, orient), (w, h, "")]:
            if key in known_models and known_models[key]:
                it["model"] = known_models[key].most_common(1)[0][0]
                filled += 1
                break

    print(f"  Dimension-based model inference: {filled} items filled\n")

    # ── Spatial deduplication across drawings ──
    # Same floor + same (cat, model, w, h, orient) + within 100mm → same device
    DEDUP_TOLERANCE_MM = 100

    # Build dedup key: (floor, cat, model, width, height, orient)
    dedup_groups = defaultdict(list)
    for it in all_inserts:
        key = (it["floor"], it["cat"], it.get("model", ""), it["width"], it["height"], it["orient"])
        dedup_groups[key].append(it)

    deduped = []
    merged_count = 0
    for key, items in dedup_groups.items():
        # Cluster by spatial proximity within this group
        clustered = []  # list of clusters, each cluster is a list of items
        for it in items:
            x, y = it["x"], it["y"]
            found = False
            for cluster in clustered:
                # Check if this item is within tolerance of any item in the cluster
                for c in cluster:
                    if abs(x - c["x"]) < DEDUP_TOLERANCE_MM and abs(y - c["y"]) < DEDUP_TOLERANCE_MM:
                        cluster.append(it)
                        found = True
                        break
                if found:
                    break
            if not found:
                clustered.append([it])

        for cluster in clustered:
            if len(cluster) == 1:
                deduped.append(cluster[0])
            else:
                # Merge: keep first, combine sources, use max qty
                first = cluster[0]
                sources = sorted(set(it["source"] for it in cluster))
                first["source"] = ", ".join(sources)
                first["qty"] = max(it.get("qty", 1) for it in cluster)
                deduped.append(first)
                merged_count += len(cluster) - 1

    all_inserts = deduped
    print(f"  Spatial dedup: {merged_count} duplicates merged (tolerance={DEDUP_TOLERANCE_MM}mm)")

    # ── Separate DCC equipment vs others ──
    dcc_eq = [it for it in all_inserts if it["cat"] == "DCC设备"]
    dcc_rack = [it for it in all_inserts if it["cat"] == "DCC支架"]
    dcc_div = [it for it in all_inserts if it["cat"] == "DCC隔断/门"]
    doors = [it for it in all_inserts if it["cat"] in ("检修门", "单扇防火门", "双扇防火门", "卷帘门", "铝合金门", "门")]
    struct = [it for it in all_inserts if it["cat"] in ("柱", "混凝土柱", "钢柱", "钢梁", "梁", "型钢", "柱底板/垫板")]
    fasteners = [it for it in all_inserts if it["cat"] == "紧固件/螺栓"]

    print(f"  DCC设备: {len(dcc_eq)}  |  DCC支架: {len(dcc_rack)}  |  门: {len(doors)}  |  结构: {len(struct)}")

    # ═══════════════════════════════════
    # GENERATE MASTER TABLE
    # ═══════════════════════════════════

    print("\n" + "=" * 70)
    print("PHASE 5: Generating master table")
    print("=" * 70)

    # Find next available version
    v = 1
    while (OUT_DIR / f"设备材料表_v{v}.csv").exists():
        v += 1
    out = OUT_DIR / f"设备材料表_v{v}.csv"

    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        cw = csv.writer(f)
        seq = [0]

        def next_seq():
            seq[0] += 1
            return seq[0]

        def section(title, headers):
            cw.writerow([])
            cw.writerow([title])
            cw.writerow(headers)

        def write_group(items, group_key_fn, extra_cols=None):
            """Group items, sort, write rows. Returns list of rows written."""
            groups = defaultdict(list)
            for it in items:
                key = group_key_fn(it)
                groups[key].append(it)

            written = 0
            for key in sorted(groups, key=lambda k: groups[k][0].get("model_sort", 99)):
                instances = groups[key]
                first = instances[0]
                row = [
                    next_seq(),
                    first.get("axis", ""),
                    first.get("zone", ""),
                    first.get("model", ""),
                    first.get("width", ""),
                    first.get("height", ""),
                    first.get("orient", ""),
                    "",  # install type
                    len(instances),
                ]
                if extra_cols:
                    row.extend(extra_cols(first, instances))
                cw.writerow(row)
                written += 1
            return written

        # ── DCC Equipment ──
        section("一、DCC通风设备",
                ["序号", "轴位", "区域", "编号", "宽度/m", "高度/m", "左右接", "安装方式", "数量/台", "来源图纸"])
        for it in dcc_eq:
            it["model_sort"] = model_sort_key(it["model"])

        def dcc_key(it):
            return (model_sort_key(it["model"]), -float(it["width"] or 0), it["orient"] or "", it["axis"] or "",
                    it["zone"] or "")

        dcc_groups = defaultdict(list)
        for it in dcc_eq:
            key = (it["model"], it["width"], it["height"], it["orient"], it["axis"], it["zone"])
            dcc_groups[key].append(it)

        for (model, w, h, orient, axis, zone), instances in sorted(dcc_groups.items(),
                key=lambda x: (model_sort_key(x[0][0]), -float(x[0][1] or 0), x[0][3] or '', x[0][4] or '')):
            sources = sorted(set(it["source"] for it in instances))
            src_str = ", ".join(sources[:3])
            if len(sources) > 3:
                src_str += f" +{len(sources)-3}"
            cw.writerow([next_seq(), axis, zone, model, w, h, orient, "",
                         sum(it.get("qty", 1) for it in instances), src_str])

        # ── DCC Racks ──
        section("二、DCC支架",
                ["序号", "轴位", "区域", "编号", "宽度/m", "高度/m", "左右接", "安装方式", "数量/台", "来源图纸"])
        rack_groups = defaultdict(list)
        for it in dcc_rack:
            key = (it["model"], it["width"], it["height"], it["orient"], it["axis"], it["zone"])
            rack_groups[key].append(it)
        for (model, w, h, orient, axis, zone), instances in sorted(rack_groups.items(),
                key=lambda x: (model_sort_key(x[0][0]), -float(x[0][1] or 0))):
            sources = sorted(set(it["source"] for it in instances))
            cw.writerow([next_seq(), axis, zone, model, w, h, orient, "",
                         sum(it.get("qty", 1) for it in instances),
                         ", ".join(sources[:2])])

        # ── Doors ──
        section("三、门",
                ["序号", "轴位", "区域", "类别", "名称", "规格", "数量/樘", "来源图纸"])
        door_groups = defaultdict(list)
        for it in doors:
            door_groups[(it["cat"], it["block_name"], it["spec"])].append(it)
        for (cat, name, spec), instances in sorted(door_groups.items(), key=lambda x: -len(x[1])):
            sources = sorted(set(it["source"] for it in instances))
            cw.writerow([next_seq(), "", "", cat, name, spec, len(instances),
                         ", ".join(sources[:2])])

        # ── Structure ──
        section("四、结构构件",
                ["序号", "轴位", "区域", "类别", "名称", "规格", "数量", "来源图纸"])
        struct_groups = defaultdict(list)
        for it in struct:
            struct_groups[(it["cat"], it["block_name"], it["spec"])].append(it)
        for (cat, name, spec), instances in sorted(struct_groups.items(), key=lambda x: -len(x[1])):
            sources = sorted(set(it["source"] for it in instances))
            cw.writerow([next_seq(), "", "", cat, name, spec, len(instances),
                         ", ".join(sources[:2])])

        # ── Fasteners ──
        section("五、紧固件/螺栓",
                ["序号", "轴位", "区域", "类别", "名称", "规格", "数量", "来源图纸"])
        fast_groups = defaultdict(list)
        for it in fasteners:
            fast_groups[(it["block_name"], it["spec"])].append(it)
        for (name, spec), instances in sorted(fast_groups.items(), key=lambda x: -len(x[1])):
            sources = sorted(set(it["source"] for it in instances))
            cw.writerow([next_seq(), "", "", "紧固件/螺栓", name, spec, len(instances),
                         ", ".join(sources[:2])])

        # ── Per-file summary ──
        section("六、各图纸汇总",
                ["图纸", "DCC设备", "DCC支架", "隔断/门", "门", "结构", "紧固件", "类型"])
        def belongs_to(item, src):
            """Check if item originates from src (handles merged multi-source)."""
            return src in item["source"].split(", ")
        for data in all_data:
            src = data["stem"]
            dcc_eq_n = sum(it.get("qty", 1) for it in dcc_eq if belongs_to(it, src))
            dcc_rk_n = sum(it.get("qty", 1) for it in dcc_rack if belongs_to(it, src))
            dcc_dv_n = sum(1 for it in dcc_div if belongs_to(it, src))
            door_n = sum(1 for it in doors if belongs_to(it, src))
            struct_n = sum(1 for it in struct if belongs_to(it, src))
            fast_n = sum(1 for it in fasteners if belongs_to(it, src))
            ftype = ""
            if "KEQP" in src: ftype = "厨房设备"
            elif "KPART" in src: ftype = "厨房隔断"
            elif "AR" in src: ftype = "建筑"
            cw.writerow([src, dcc_eq_n, dcc_rk_n, dcc_dv_n, door_n, struct_n, fast_n, ftype])

    print(f"\n  Master table: {out}")
    print(f"  DCC设备 {len(dcc_eq)}个 | DCC支架 {len(dcc_rack)}个 | 门 {len(doors)}樘 | 结构 {len(struct)}个 | 紧固件 {len(fasteners)}个")

    # Also generate install schedule
    print("\nGenerating install_schedule.csv...")
    sched_out = OUT_DIR / "install_schedule.csv"
    with open(sched_out, "w", newline="", encoding="utf-8-sig") as f:
        cw = csv.writer(f)
        cw.writerow(["序号", "来源图纸", "类别", "构件名称", "规格", "编号", "轴位", "区域",
                      "宽度/m", "高度/m", "左右接", "X坐标", "Y坐标", "图层"])
        for i, it in enumerate(all_inserts, 1):
            cw.writerow([i, it["source"], it["cat"], it["block_name"], it["spec"],
                        it["model"], it["axis"], it["zone"],
                        it["width"], it["height"], it["orient"],
                        it["x"], it["y"], it["layer"]])
    print(f"  Install schedule: {sched_out}")
    print(f"  {len(all_inserts)} items with axis + zone assignments")

    print("\nDONE!")


if __name__ == "__main__":
    main()
