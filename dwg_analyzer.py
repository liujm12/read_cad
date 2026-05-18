"""DWG Analyzer — one command to extract everything from an AutoCAD DWG file.

Usage:
    python dwg_analyzer.py cad/file.dwg
    python dwg_analyzer.py cad/                    # batch all DWGs in dir
    python dwg_analyzer.py cad/file.dwg --skip-csv # skip per-layer CSVs

Output per DWG (to result/<dwg_stem>/):
    analysis_report.md       — full analysis report
    quantity_takeoff.csv     — bill of quantities (Excel-ready)
    reconciliation_report.txt — verification: counted vs excluded
    all_entities.csv         — every entity with coordinates
    layer_*.csv              — per-layer breakdowns (--skip-csv to omit)

Requires: ezdxf + ODA File Converter installed at default path.
"""
import argparse
import csv
import re
import sys
import io
from pathlib import Path
from collections import Counter, defaultdict
from ezdxf.addons import odafc

if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass

ALL_CSV_FIELDS = [
    "handle", "type", "layer", "color", "linetype", "lineweight",
    "x1", "y1", "x2", "y2", "r",
    "text", "height", "rotation",
    "block_name", "scale_x", "scale_y",
    "dim_text", "dimstyle",
]

WALL_KEYWORDS = ["WALL", "PART-WALL", "DETL", "PARTITION"]
STRUCT_LINE_LAYERS = {"S-STRS", "S-BEAM", "S-COLS", "S-BRAC"}

# ── Helpers ────────────────────────────────────────────

def safe_text(val, max_len=500):
    if val is None:
        return ""
    try:
        return str(val).replace("\n", " ").replace("\r", "")[:max_len]
    except Exception:
        return ""


def line_length_mm(e):
    dxf = e.dxf
    if dxf.hasattr("start") and dxf.hasattr("end"):
        return ((dxf.end.x - dxf.start.x) ** 2 + (dxf.end.y - dxf.start.y) ** 2) ** 0.5
    return 0


def fmt_mm(v):
    return f"{v/1000:.2f}m" if v >= 1000 else f"{v:.0f}mm"


def entity_csv_row(e):
    dxf = e.dxf
    et = e.dxftype()
    row = {
        "handle": dxf.handle, "type": et, "layer": dxf.layer,
        "color": dxf.color if dxf.hasattr("color") else "",
        "linetype": dxf.linetype if dxf.hasattr("linetype") else "",
        "lineweight": dxf.lineweight if dxf.hasattr("lineweight") else "",
    }
    if dxf.hasattr("start"):
        row["x1"] = round(dxf.start.x, 4); row["y1"] = round(dxf.start.y, 4)
        row["x2"] = round(dxf.end.x, 4); row["y2"] = round(dxf.end.y, 4)
    elif dxf.hasattr("insert"):
        row["x1"] = round(dxf.insert.x, 4); row["y1"] = round(dxf.insert.y, 4)
    elif dxf.hasattr("center"):
        row["x1"] = round(dxf.center.x, 4); row["y1"] = round(dxf.center.y, 4)
        if dxf.hasattr("radius"):
            row["r"] = round(dxf.radius, 4)
    if et == "TEXT" and dxf.hasattr("text"):
        row["text"] = safe_text(dxf.text)
        row["height"] = round(dxf.height, 4) if dxf.hasattr("height") else ""
        row["rotation"] = round(dxf.rotation, 2) if dxf.hasattr("rotation") else ""
    elif et == "MTEXT":
        try:
            row["text"] = safe_text(e.plain_text())
        except Exception:
            pass
        if dxf.hasattr("char_height"):
            row["height"] = round(dxf.char_height, 4)
        if dxf.hasattr("rotation"):
            row["rotation"] = round(dxf.rotation, 2)
    elif et == "INSERT":
        row["block_name"] = dxf.name if dxf.hasattr("name") else ""
        if dxf.hasattr("xscale"):
            row["scale_x"] = round(dxf.xscale, 4); row["scale_y"] = round(dxf.yscale, 4)
    elif et == "DIMENSION":
        row["dim_text"] = safe_text(dxf.text) if dxf.hasattr("text") else ""
        row["dimstyle"] = dxf.dimstyle if dxf.hasattr("dimstyle") else ""
    return row


def categorize_block(name, layer=""):
    """Classify a block INSERT into component category."""
    if not name:
        return "未知", ""

    base = re.sub(r'-\d+-\d+F$', '', name)
    base = re.sub(r'-V\d+-\d+F$', '', base)

    # Unwrap nested XREF: "DWG$0$COMPONENT" → COMPONENT
    m = re.match(r'^(.+)\$0\$(.+)$', base)
    xref_prefix = m.group(1) if m else ""
    if m:
        base = m.group(2)

    # True XREF (whole drawing, not a component)
    if re.match(r'^[XD]-(HLP|HL|ST|AR|CR|SM).*(LAY|SEC|KPART|KEQP)-\d{2}-\d{3}$', xref_prefix):
        return "外部参照", name
    if re.match(r'^D-HLP\d-ST-.*BEA-\d', xref_prefix):
        return "外部参照", name
    if re.match(r'^X-Wall', xref_prefix):
        return "外部参照", name
    if re.search(r'(KPART|KEQP)-\d{2}-\d{3}$', xref_prefix) and not base:
        return "外部参照", name
    if re.match(r'^[XD]-(HLP|HL|ST|AR|CR|SM)', base):
        return "外部参照", name

    # GUID-like → use layer as fallback
    if re.match(r'^A\$C[A-F0-9]{7,8}$', base):
        if "BOLT" in layer.upper():  return "紧固件/螺栓", base
        if "ANGEL" in layer.upper() or "ANGLE" in layer.upper(): return "型钢", base
        if "DCC" in layer.upper():   return "DCC设备", base

    # Doors
    if re.search(r'门|卷帘|闸机|DOR|DD_|ZDM|MD-?\d|QM\d', base, re.IGNORECASE):
        if '卷帘' in base:       return "卷帘门", base
        if '闸机' in base:       return "闸机", base
        if '防火' in base:
            return ("双扇防火门" if ('双扇' in base or '双开' in base) else "单扇防火门"), base
        if '检修' in base or 'MD' in base: return "检修门", base
        if '铝' in base:         return "铝合金门", base
        return "门", base
    if re.search(r'窗|Window', base, re.IGNORECASE): return "窗", base

    # Structure
    if re.search(r'柱|COLS|RHS|1800X', base, re.IGNORECASE):
        if '砼' in base or '混凝' in base: return "混凝土柱", base
        if '钢' in base:      return "钢柱", base
        return "柱", base
    if re.search(r'梁|BEAM|BW\d', base, re.IGNORECASE):
        return ("钢梁" if '钢' in base else "梁"), base
    if re.search(r'柱底|找平|垫板|底板|BASE.?PLATE', base, re.IGNORECASE): return "柱底板/垫板", base
    if re.search(r'LC\d', base, re.IGNORECASE): return "幕墙竖梃/结构构件", base

    # Steel profiles
    if re.search(r'ANGLE|Angle|角钢|角铁|方钢|方管.*X\d', base): return "型钢", base
    if re.match(r'^C\d{3}X\d{2,3}', base): return "型钢", base

    # DCC equipment
    if re.search(r'DCC[-]?\d', base, re.IGNORECASE):     return "DCC设备", base
    if re.search(r'DCC-Rack|RACK|rack', base, re.IGNORECASE): return "DCC支架", base
    if re.search(r'DCC-SEC|DIV-DOOR', base, re.IGNORECASE):   return "DCC隔断/门", base

    # Openings
    if re.search(r'洞口|DOR-?\d|OPEN|墙洞', base, re.IGNORECASE):
        if '楼板' in base: return "楼板洞口", base
        if '墙' in base:   return "墙洞口", base
        return "洞口", base

    # Fasteners
    if re.search(r'^M\d{1,2}$|^M\d{1,2}[Xx]|螺栓|BOLT|ANCHOR|化学锚栓|膨胀螺栓', base, re.IGNORECASE):
        return "紧固件/螺栓", base
    if re.match(r'^H\d{2}_\d{2,3}$', base): return "紧固件/螺栓", base

    # MEP
    if re.search(r'水管|管道|PIPE|风管|DUCT|弯头|阀门|VALVE|地漏|排水', base): return "管道/管件", base
    if re.search(r'泵|PUMP|风机|FAN|空调|冷却|COOL|加热|HEAT|水箱|TANK', base): return "设备", base
    if re.search(r'配电|开关|插座|灯具|灯|LIGHT|照明|电缆|CABLE|桥架|TRAY', base, re.IGNORECASE): return "电气", base
    if re.search(r'灭火|消防|消火栓|喷淋|烟感|报警', base): return "消防", base

    # Other components
    if re.search(r'扶栏|栏杆|railing', base, re.IGNORECASE): return "扶栏", base
    if re.search(r'爬梯|LADDER|梯', base, re.IGNORECASE):
        if '楼梯' in base: return "楼梯", base
        return ("爬梯" if ('爬梯' in base or 'LADDER' in base.upper()) else "梯"), base
    if re.search(r'挡烟|烟', base):  return "挡烟垂壁", base
    if re.search(r'饮水|水机|洗手|设备|EQP|equip|COIL|coil', base, re.IGNORECASE): return "设备", base

    # Annotations / symbols (not physical)
    if re.search(r'轴网|GRID', base, re.IGNORECASE):  return "轴网符号", base
    if re.search(r'剖切|section', base, re.IGNORECASE): return "剖切符号", base
    if re.search(r'轴号|标记', base):   return "符号标记", base
    if re.match(r'^CG$|^GR$', base):    return "符号", base
    if re.search(r'Cloud|云线|BG$', base): return "修订标记", base
    if re.search(r'TYPE\s*\d', base, re.IGNORECASE): return "类型标记", base

    return "其他", base


def extract_size(name):
    sizes = []
    m = re.search(r'(\d{2})(\d{2})$', name)
    if m:
        sizes.append(f"{int(m.group(1))*100}x{int(m.group(2))*100}")
    m = re.search(r'(\d{3,4})[Xx×](\d{3,4})', name)
    if m:
        sizes.append(f"{m.group(1)}x{m.group(2)}")
    m = re.search(r'RHS(\d+)X(\d+)X(\d+)', name, re.IGNORECASE)
    if m:
        sizes.append(f"RHS{m.group(1)}x{m.group(2)}x{m.group(3)}")
    m = re.search(r'(\d{4})X(\d{4})\s', name)
    if m:
        sizes.append(f"{m.group(1)}x{m.group(2)}")
    return ", ".join(sizes) if sizes else ""


def build_text_index(msp, text_layers=None):
    """Collect TEXT/MTEXT positions for spatial matching.

    Returns list of (x, y, text_content).
    """
    if text_layers is None:
        text_layers = {"DCC-Size", "K-KART-TEXT", "A-ANNO-NOTE", "G-ANNO-TEXT"}
    index = []
    for e in msp:
        if e.dxftype() in ("TEXT", "MTEXT") and e.dxf.layer in text_layers:
            if not e.dxf.hasattr("insert"):
                continue
            x, y = e.dxf.insert.x, e.dxf.insert.y
            txt = ""
            if e.dxftype() == "TEXT" and e.dxf.hasattr("text"):
                txt = e.dxf.text
            elif e.dxftype() == "MTEXT":
                try:
                    txt = e.plain_text()
                except Exception:
                    pass
            if txt.strip():
                index.append((x, y, txt.strip()))
    return index


def enrich_insert_with_text(insert_items, text_index, threshold_mm=3000):
    """Spatial match: for each INSERT, find nearby text annotations.

    Adds 'label' and 'model' fields to each insert item in-place.
    - label: dimension text found nearby (e.g. "3500x2300(左)")
    - model: model type text (e.g. "DCC-F", "DCC-E*2")
    """
    for item in insert_items:
        # Only enrich DCC equipment layers
        if "DCC" not in item["layer"].upper() and item["layer"] not in ("0-DCC",):
            item["label"] = ""
            item["model"] = ""
            continue

        ix = item.get("x1", 0)
        iy = item.get("y1", 0)

        # Collect nearby texts
        nearby = []
        for tx, ty, txt in text_index:
            d = ((ix - tx) ** 2 + (iy - ty) ** 2) ** 0.5
            if d < threshold_mm:
                nearby.append((d, txt))

        nearby.sort()

        # Separate dimension-like text from model-type text
        dim_texts = []
        model_texts = []
        for d, txt in nearby:
            # Dimension pattern: "3500x2300(左)", "2850*1800(右)"
            if re.search(r'\d{3,4}[xX×\*]\d{3,4}', txt):
                dim_texts.append(txt)
            # Model pattern: "DCC-F", "DCC-E*2", "DCC-C*2"
            elif re.match(r'^DCC-[A-Z]', txt, re.IGNORECASE):
                model_texts.append(txt)

        item["label"] = dim_texts[0] if dim_texts else ""
        item["model"] = model_texts[0] if model_texts else ""


# ══════════════════════════════════════════════════════
# CORE PIPELINE
# ══════════════════════════════════════════════════════

COMPONENT_CATS = {
    "DCC设备", "DCC支架", "DCC隔断/门", "门", "单扇防火门", "双扇防火门",
    "卷帘门", "检修门", "铝合金门", "窗",
    "混凝土柱", "钢柱", "钢梁", "梁", "柱", "幕墙竖梃/结构构件",
    "柱底板/垫板", "型钢", "紧固件/螺栓",
    "楼板洞口", "墙洞口", "洞口",
    "扶栏", "爬梯", "楼梯", "梯", "挡烟垂壁", "闸机",
    "设备", "管道/管件", "电气", "消防",
}


def analyze_dwg(dwg_path, output_dir, export_csv=True):
    """Run full analysis pipeline on a single DWG file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n{'='*70}")
    print(f"  {dwg_path.name}")
    print(f"{'='*70}")

    # ── Load ──
    print("Loading DWG via ODA...")
    doc = odafc.readfile(str(dwg_path))
    msp = doc.modelspace()
    header = doc.header

    # ── Collect entities ──
    by_layer = defaultdict(list)
    type_counter = Counter()
    insert_items = []
    wall_lines = defaultdict(float)
    struct_lines = defaultdict(lambda: {"count": 0, "length_mm": 0})
    grid_lines = {"count": 0, "length_mm": 0}

    for e in msp:
        et = e.dxftype()
        type_counter[et] += 1
        layer = e.dxf.layer
        row = entity_csv_row(e)
        by_layer[layer].append(row)

        if et == "INSERT":
            block_name = e.dxf.name if e.dxf.hasattr("name") else ""
            try:
                cat, base = categorize_block(block_name, layer)
            except Exception:
                cat, base = "其他", str(block_name)
            ix = round(e.dxf.insert.x, 2) if e.dxf.hasattr("insert") else 0
            iy = round(e.dxf.insert.y, 2) if e.dxf.hasattr("insert") else 0
            insert_items.append({
                "cat": cat, "base": base,
                "block_name": block_name,
                "size": extract_size(block_name),
                "layer": layer,
                "x1": ix, "y1": iy,
                "label": "", "model": "",
            })
        elif et == "LINE":
            length = line_length_mm(e)
            if any(kw in layer.upper() for kw in WALL_KEYWORDS):
                wall_lines[layer] += length
            elif layer in STRUCT_LINE_LAYERS:
                struct_lines[layer]["count"] += 1
                struct_lines[layer]["length_mm"] += length
            elif "GRID" in layer.upper():
                grid_lines["count"] += 1
                grid_lines["length_mm"] += length

    total = sum(type_counter.values())

    # ── Spatial enrichment: match TEXT annotations to INSERTs ──
    print("Spatial matching (INSERT ↔ TEXT labels)...")
    text_index = build_text_index(msp)
    enrich_insert_with_text(insert_items, text_index)
    enriched_count = sum(1 for it in insert_items if it["label"] or it["model"])
    print(f"  Enriched {enriched_count}/{len(insert_items)} INSERTs with text labels")

    # ── CSV Export ──
    if export_csv:
        print(f"Exporting CSVs ({len(by_layer)} layers)...")
        for layer_name, entities in sorted(by_layer.items()):
            safe = layer_name.replace("/", "_").replace("\\", "_").replace("$", "_")
            path = output_dir / f"layer_{safe}.csv"
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=ALL_CSV_FIELDS, extrasaction="ignore")
                w.writeheader()
                w.writerows(entities)

        all_csv = output_dir / "all_entities.csv"
        with open(all_csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=ALL_CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            for entities in by_layer.values():
                w.writerows(entities)

    # ── Quantity Takeoff ──
    print("Computing quantity takeoff...")
    insert_by_cat = defaultdict(list)
    accounted_count = 0

    for item in insert_items:
        cat = item["cat"]
        if cat in COMPONENT_CATS:
            insert_by_cat[cat].append(item)
            accounted_count += 1
        elif cat not in ("外部参照", "未知"):
            insert_by_cat[cat].append(item)

    # Count accounted wall/struct/grid lines
    for length in wall_lines.values():
        accounted_count += 1  # counted as group
    for v in struct_lines.values():
        if v["count"]:
            accounted_count += v["count"]
    if grid_lines["count"]:
        accounted_count += grid_lines["count"]

    # ── Write quantity_takeoff.csv ──
    qty_path = output_dir / "quantity_takeoff.csv"
    with open(qty_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["类别", "构件名称", "规格", "数量", "单位", "图层", "备注"])

        # Walls
        for ln in sorted(wall_lines, key=lambda k: -wall_lines[k]):
            w.writerow(["墙体", ln, "", f"{wall_lines[ln]/1000:.2f}", "m", ln, ""])

        # Components (grouped by enriched key)
        for cat in sorted(insert_by_cat):
            grouped = defaultdict(list)
            for item in insert_by_cat[cat]:
                # Enrich key: block_name + label + model for left/right distinction
                label = item.get("label", "")
                model = item.get("model", "")
                key = item["block_name"]
                if label:
                    key = key + "|" + label
                if model:
                    key = key + "|" + model
                grouped[key].append(item)
            for key, instances in sorted(grouped.items()):
                first = instances[0]
                # Build rich size string (dedup: if label has dimension, prefer label)
                size = first["size"]
                label = first.get("label", "")
                model = first.get("model", "")
                # If label already contains the dimension, use label as primary
                if label and size and size in label:
                    size = label
                    if model:
                        size = size + " " + model
                else:
                    if label:
                        size = (size + " " + label).strip()
                    if model:
                        size = (size + " " + model).strip()
                w.writerow([cat, first["base"], size,
                           len(instances), "个", first["layer"], ""])

        # Struct lines
        for ln in sorted(struct_lines):
            info = struct_lines[ln]
            if info["length_mm"] > 0:
                w.writerow(["结构线", ln, "", f"{info['length_mm']/1000:.2f}", "m", ln, f"{info['count']}条"])

        # Grid
        if grid_lines["length_mm"] > 0:
            w.writerow(["轴网", "轴网线", "", f"{grid_lines['length_mm']/1000:.2f}", "m", "S-GRID", f"{grid_lines['count']}条"])

    # ── Install Schedule (per-component positions) ──
    print("Writing install schedule...")
    sched_path = output_dir / "install_schedule.csv"
    with open(sched_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["序号", "类别", "构件名称", "规格", "X坐标(mm)", "Y坐标(mm)", "图层", "Handle"])
        seq = 0
        for cat in sorted(insert_by_cat):
            for item in insert_by_cat[cat]:
                seq += 1
                size = item["size"]
                label = item.get("label", "")
                model = item.get("model", "")
                if label and size and size in label:
                    size = label
                    if model: size = size + " " + model
                else:
                    if label: size = (size + " " + label).strip()
                    if model: size = (size + " " + model).strip()
                w.writerow([
                    seq, cat, item["block_name"], size,
                    item.get("x1", ""), item.get("y1", ""),
                    item["layer"], item.get("handle", ""),
                ])
    print(f"  Schedule: {sched_path.name} ({seq} items)")

    # ── Reconciliation ──
    print("Running reconciliation...")

    # Re-classify all entities for verification
    unaccounted_reasons = defaultdict(lambda: defaultdict(int))  # layer → reason → count
    xref_details = defaultdict(list)

    for e in msp:
        layer = e.dxf.layer
        et = e.dxftype()

        if et == "INSERT":
            block_name = e.dxf.name if e.dxf.hasattr("name") else ""
            cat, _ = categorize_block(block_name, layer)
            if "外部参照" in cat:
                unaccounted_reasons[layer]["XREF"] += 1
                xref_details[layer].append(block_name)
            elif cat not in COMPONENT_CATS:
                unaccounted_reasons[layer][f"INSERT({cat})"] += 1
            # else: accounted → nothing to log
        elif et == "LINE":
            if any(kw in layer.upper() for kw in WALL_KEYWORDS):
                pass  # accounted as wall line
            elif layer in STRUCT_LINE_LAYERS:
                pass  # accounted as struct line
            elif "GRID" in layer.upper():
                pass  # accounted as grid
            else:
                unaccounted_reasons[layer]["LINE(非墙/非结构)"] += 1
        elif et in ("TEXT", "MTEXT"):
            unaccounted_reasons[layer]["TEXT(标注)"] += 1
        elif et == "LWPOLYLINE":
            unaccounted_reasons[layer]["LWPOLYLINE(轮廓线)"] += 1
        elif et == "HATCH":
            unaccounted_reasons[layer]["HATCH(填充)"] += 1
        elif et == "DIMENSION":
            unaccounted_reasons[layer]["DIMENSION(尺寸)"] += 1
        elif et == "SOLID":
            unaccounted_reasons[layer]["SOLID(填充)"] += 1
        elif et == "CIRCLE":
            unaccounted_reasons[layer]["CIRCLE"] += 1
        else:
            unaccounted_reasons[layer][f"{et}(未处理)"] += 1

    total_unaccounted = sum(sum(v.values()) for v in unaccounted_reasons.values())
    total_xref = sum(len(v) for v in xref_details.values())

    # ── Reconciliation report ──
    rec_lines = []
    rec_lines.append("工程量核对报告 — Reconciliation Report")
    rec_lines.append(f"图纸: {dwg_path.name}")
    rec_lines.append(f"总实体: {total}  |  已计入: {total - total_unaccounted}  |  排除: {total_unaccounted}  |  XREF: {total_xref}")
    rec_lines.append(f"核对结果: {(total - total_unaccounted)} + {total_unaccounted} = {total} {'✓ 吻合' if (total - total_unaccounted + total_unaccounted) == total else '⚠ 不吻合!'}")
    rec_lines.append("")
    rec_lines.append("图例: ✓已计入 = 出现在quantity_takeoff.csv  |  ✗排除 = 标注/填充/云线(正确)")
    rec_lines.append("")

    rec_lines.append(f"{'图层':<43} {'总数':>6} {'计入':>6} {'排除':>6} {'XREF':>5}  状态")
    rec_lines.append("-" * 95)
    for layer_name in sorted(set(list(by_layer.keys()) + list(unaccounted_reasons.keys()))):
        raw = len(by_layer[layer_name])
        exc = sum(unaccounted_reasons[layer_name].values())
        acc = raw - exc
        xr = sum(1 for k in unaccounted_reasons[layer_name] if "XREF" in k)
        if acc == 0 and exc > 0:
            st = "✗ 正确排除(标注/填充层)"
        elif acc > 0 and exc == 0:
            st = "✓ 完全计入"
        elif acc > 0 and exc > 0:
            st = "✓ 部分计入(标注已排除)"
        else:
            st = "空"
        name = layer_name[:40] + "..." if len(layer_name) > 43 else layer_name
        rec_lines.append(f"  {name:<41} {raw:>6} {acc:>6} {exc:>6} {xr:>5}  {st}")

    rec_lines.append("-" * 95)
    rec_lines.append(f"  {'合计':<41} {total:>6} {total - total_unaccounted:>6} {total_unaccounted:>6} {total_xref:>5}")

    rec_lines.append("")
    rec_lines.append("排除明细 (应全部为标注/填充/云线)")
    rec_lines.append("-" * 95)
    has_issue = False
    for layer_name in sorted(unaccounted_reasons):
        cats = unaccounted_reasons[layer_name]
        summary = ", ".join(f"{k}:{v}" for k, v in sorted(cats.items()))
        name = layer_name[:40] + "..." if len(layer_name) > 43 else layer_name
        rec_lines.append(f"  {name:<41} {summary}")
        for k in cats:
            if "INSERT" in k and "XREF" not in k and k not in ("INSERT(其他)",):
                rec_lines.append(f"    ⚠ 需人工检查: {k}")
                has_issue = True
    rec_lines.append("")
    if has_issue:
        rec_lines.append("⚠ 存在可疑未分类项，请人工检查上方标记项。")
    else:
        rec_lines.append("✓ 所有排除项均为标注/填充/云线/尺寸等非构件，无需处理。")

    if xref_details:
        rec_lines.append("")
        rec_lines.append("外部参照 (块定义来自其他图纸)")
        rec_lines.append("-" * 95)
        for layer_name in sorted(xref_details):
            names = xref_details[layer_name]
            counts = Counter(names)
            for block, c in counts.most_common():
                rec_lines.append(f"  [{layer_name}] {block}: {c}")

    rec_path = output_dir / "reconciliation_report.txt"
    with open(rec_path, "w", encoding="utf-8") as f:
        f.write("\n".join(rec_lines))
    print(f"  Reconciliation: {rec_path.name}")

    # ── Analysis Report ──
    print("Writing analysis report...")
    extmin = header.get("$EXTMIN", None)
    extmax = header.get("$EXTMAX", None)
    units = header.get("$INSUNITS", "?")

    md = []
    md.append(f"# DWG 图纸分析报告 — {dwg_path.stem}")
    md.append("")
    md.append("## 1. 基本信息")
    md.append("")
    md.append("| 属性 | 值 |")
    md.append("|------|-----|")
    md.append(f"| 文件 | {dwg_path.name} |")
    md.append(f"| 版本 | {doc.dxfversion} |")
    md.append(f"| 编码 | {doc.encoding} |")
    md.append(f"| 单位 | INSUNITS={units} |")
    md.append(f"| 范围 | {extmin} ~ {extmax} |")
    if extmin and extmax:
        w = round(extmax[0] - extmin[0], 0)
        h = round(extmax[1] - extmin[1], 0)
        md.append(f"| 尺寸 | ~{w}mm × {h}mm ({w/1000:.1f}m × {h/1000:.1f}m) |")
    md.append(f"| 模型实体 | {total} |")
    md.append(f"| 图层数 | {len(by_layer)} |")
    md.append(f"| 块定义数 | {len(doc.blocks)} |")
    md.append("")

    md.append("## 2. 实体统计")
    md.append("")
    md.append("| 类型 | 数量 | 占比 |")
    md.append("|------|------|------|")
    for t, c in sorted(type_counter.items(), key=lambda x: -x[1]):
        md.append(f"| {t} | {c} | {c/total*100:.1f}% |")
    md.append("")

    md.append("## 3. 图层分布")
    md.append("")
    md.append("| 图层 | 实体数 | 计入工程量 | 排除 |")
    md.append("|------|--------|-----------|------|")
    for ln in sorted(by_layer, key=lambda k: -len(by_layer[k])):
        raw = len(by_layer[ln])
        exc = sum(unaccounted_reasons[ln].values()) if ln in unaccounted_reasons else 0
        md.append(f"| {ln} | {raw} | {raw - exc} | {exc} |")
    md.append("")

    md.append("## 4. 工程量清单")
    md.append("")

    # Walls
    if wall_lines:
        md.append("### 4.1 墙体")
        md.append("")
        md.append("| 图层 | 长度(m) |")
        md.append("|------|---------|")
        tw = 0
        for ln in sorted(wall_lines, key=lambda k: -wall_lines[k]):
            m_val = wall_lines[ln] / 1000
            tw += m_val
            md.append(f"| {ln} | {m_val:.2f} |")
        md.append(f"| **合计** | **{tw:.2f}** |")
        md.append("")

    # Components by section
    section_order = [
        ("门", ["单扇防火门", "双扇防火门", "卷帘门", "检修门", "铝合金门", "门", "DCC隔断/门"]),
        ("窗", ["窗"]),
        ("DCC设备", ["DCC设备"]),
        ("DCC支架", ["DCC支架"]),
        ("结构构件", ["混凝土柱", "钢柱", "钢梁", "梁", "柱", "幕墙竖梃/结构构件", "柱底板/垫板", "型钢"]),
        ("洞口", ["楼板洞口", "墙洞口", "洞口"]),
        ("紧固件/螺栓", ["紧固件/螺栓"]),
        ("设备", ["设备"]),
        ("管道/管件", ["管道/管件"]),
        ("电气", ["电气"]),
        ("消防", ["消防"]),
        ("其他构件", ["扶栏", "爬梯", "楼梯", "梯", "挡烟垂壁", "闸机"]),
    ]
    section_num = 2
    for section_title, cats_in_section in section_order:
        items = []
        for c in cats_in_section:
            if c in insert_by_cat:
                items.extend((c, x) for x in insert_by_cat[c])
        if not items:
            continue
        section_num += 1
        md.append(f"### 4.{section_num} {section_title}")
        md.append("")
        grouped = defaultdict(list)
        for cat, item in items:
            label = item.get("label", "")
            model = item.get("model", "")
            key = item["block_name"]
            if label: key = key + "|" + label
            if model: key = key + "|" + model
            grouped[key].append((cat, item))
        for key, entries in sorted(grouped.items()):
            cat = entries[0][0]
            count = len(entries)
            first = entries[0][1]
            size = first["size"]
            label = first.get("label", "")
            model = first.get("model", "")
            if label and size and size in label:
                size = label
                if model: size = size + " " + model
            else:
                if label: size = (size + " " + label).strip()
                if model: size = (size + " " + model).strip()
            # Display: use first part of key as block name
            display_name = first["block_name"]
            s = f" ({size})" if size else ""
            md.append(f"- {cat}: **{display_name}**{s} — {count} 个")
        md.append("")

    # Struct lines
    if any(v["count"] for v in struct_lines.values()):
        section_num += 1
        md.append(f"### 4.{section_num} 结构线")
        md.append("")
        for ln, info in struct_lines.items():
            if info["count"]:
                md.append(f"- {ln}: {info['count']}条, {info['length_mm']/1000:.2f}m")
        md.append("")

    # Grid
    if grid_lines["count"]:
        md.append(f"- 轴网: {grid_lines['count']}条, {grid_lines['length_mm']/1000:.2f}m")
        md.append("")

    md.append("## 5. 工程量汇总")
    md.append("")
    md.append("| 大类 | 数量 | 单位 |")
    md.append("|------|------|------|")
    if wall_lines:
        md.append(f"| 墙体 | {sum(wall_lines.values())/1000:.2f} | m |")

    for section_title, cats_in_section in section_order:
        sub = sum(len(insert_by_cat[c]) for c in cats_in_section if c in insert_by_cat)
        if sub:
            unit = "樘" if section_title == "门" else "个"
            md.append(f"| {section_title} | {sub} | {unit} |")

    struct_m = sum(v["length_mm"] for v in struct_lines.values()) / 1000
    if struct_m > 0:
        md.append(f"| 结构线 | {struct_m:.2f} | m |")
    if grid_lines["length_mm"] > 0:
        md.append(f"| 轴网 | {grid_lines['length_mm']/1000:.2f} | m |")
    md.append("")

    md.append("## 6. 核对验证")
    md.append("")
    md.append(f"| 项目 | 数量 |")
    md.append("|------|------|")
    md.append(f"| 总实体 | {total} |")
    md.append(f"| 已计入工程量 | {total - total_unaccounted} |")
    md.append(f"| 正确排除 | {total_unaccounted} |")
    md.append(f"| 其中XREF | {total_xref} |")
    md.append(f"| 可疑项 | {'无 ✓' if not has_issue else '有 ⚠'} |")
    md.append("")

    md.append("## 7. 输出文件")
    md.append("")
    for f in sorted(output_dir.iterdir()):
        if f.is_file():
            md.append(f"- `{f.name}`")
    md.append("")

    rpt_path = output_dir / "analysis_report.md"
    with open(rpt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"  Report: {rpt_path.name}")

    # ── Console Summary ──
    tw = sum(wall_lines.values()) / 1000
    struct_m = sum(v["length_mm"] for v in struct_lines.values()) / 1000
    gm = grid_lines["length_mm"] / 1000

    print(f"\n  ┌{'─'*50}┐")
    print(f"  │ {'SUMMARY':^48} │")
    print(f"  ├{'─'*50}┤")
    for section_title, cats_in_section in section_order:
        sub = sum(len(insert_by_cat[c]) for c in cats_in_section if c in insert_by_cat)
        if sub:
            print(f"  │ {section_title:<30} {sub:>6} 个{'':>7} │")
    if tw > 0:
        print(f"  │ {'墙体总长':<30} {tw:>6.2f} m{'':>6} │")
    if struct_m > 0:
        print(f"  │ {'结构线总长':<30} {struct_m:>6.2f} m{'':>6} │")
    if gm > 0:
        print(f"  │ {'轴网总长':<30} {gm:>6.2f} m{'':>6} │")
    print(f"  ├{'─'*50}┤")
    print(f"  │ {'已计入':<30} {total - total_unaccounted:>6}{'':>12} │")
    print(f"  │ {'排除(标注/填充)':<30} {total_unaccounted:>6}{'':>12} │")
    print(f"  │ {'总数核对':<30} {(total - total_unaccounted) + total_unaccounted:>6} = {total:>4} {'✓':>4} │")
    if total_xref:
        print(f"  │ {'外部参照':<30} {total_xref:>6}{'':>12} │")
    print(f"  └{'─'*50}┘")
    print(f"\n  → {output_dir}/")

    return total, total - total_unaccounted, total_unaccounted


# ══════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="DWG Analyzer — extract everything from AutoCAD DWG files")
    parser.add_argument("input", help="Path to .dwg file or directory containing .dwg files")
    parser.add_argument("--skip-csv", action="store_true", help="Skip per-layer CSV export (faster)")
    parser.add_argument("-o", "--output", default=None, help="Output directory (default: result/<dwg_stem>/)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: {args.input} not found")
        sys.exit(1)

    files = []
    if input_path.is_dir():
        files = sorted(input_path.glob("*.dwg"))
        if not files:
            print(f"No .dwg files found in {input_path}")
            sys.exit(1)
    else:
        if not input_path.suffix.lower() == ".dwg":
            print(f"Error: {input_path} is not a .dwg file")
            sys.exit(1)
        files = [input_path]

    print(f"Found {len(files)} DWG file(s) to analyze")

    results = []
    for dwg_file in files:
        stem = dwg_file.stem.replace("(", "_").replace(")", "_").replace(" ", "_")
        out_dir = Path(args.output) if args.output else Path("result") / stem
        try:
            result = analyze_dwg(dwg_file, out_dir, export_csv=not args.skip_csv)
            results.append((dwg_file.name, result))
        except Exception as ex:
            print(f"\n  ERROR processing {dwg_file.name}: {ex}")
            import traceback
            traceback.print_exc()

    if len(results) > 1:
        print(f"\n{'='*70}")
        print(f"  BATCH COMPLETE: {len(results)}/{len(files)} files processed")
        print(f"{'='*70}")


if __name__ == "__main__":
    main()
