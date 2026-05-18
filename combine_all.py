"""Combine all DWG analysis results into master equipment schedule."""
import csv, re
from pathlib import Path
from collections import defaultdict

ALL_DIR = Path(__file__).parent / "result" / "all"
OUT = ALL_DIR / "设备材料表_总表.csv"

def parse_spec(spec):
    w = h = orient = model = ""
    m = re.search(r'(\d{3,4})\s*[xX×\*]\s*(\d{3,4})', spec)
    if m:
        w = f"{int(m.group(1))/1000:.2f}"
        h = f"{int(m.group(2))/1000:.2f}"
    if '左' in spec: orient = '左'
    elif '右' in spec: orient = '右'
    m = re.search(r'(DCC-[A-Z]\*?\d?)', spec)
    if m: model = m.group(1)
    elif re.search(r'DCC-?\d', spec):
        m2 = re.search(r'(DCC-?\d+)', spec)
        if m2: model = m2.group(1)
    return w, h, orient, model

def model_sort(model):
    order = {'DCC-F':0,'DCC-F*2':1,'DCC-E':2,'DCC-E*2':3,'DCC-D':4,'DCC-D*2':5,
             'DCC-C':6,'DCC-C*2':7,'DCC-B':8,'DCC-B*2':9,'DCC-A':10,'DCC-A*2':11,
             'DCC-7':12,'DCC-6':13,'DCC-5':14,'DCC-J':15,'DCC-J*2':16}
    return order.get(model, 99)

def main():
    all_dcc = defaultdict(lambda: defaultdict(int))  # source_file -> (model,w,h,orient) -> count
    all_doors = defaultdict(lambda: defaultdict(int))
    all_struct = defaultdict(lambda: defaultdict(int))
    all_other = defaultdict(list)

    for subdir in sorted(ALL_DIR.iterdir()):
        if not subdir.is_dir():
            continue
        qty_csv = subdir / "quantity_takeoff.csv"
        if not qty_csv.exists():
            continue

        source = subdir.name
        with open(qty_csv, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                cat = row["类别"].strip()
                spec = row["规格"].strip()
                try:
                    qty = int(float(row["数量"].strip()))
                except (ValueError, KeyError):
                    qty = 0
                name = row["构件名称"].strip()

                if cat == "DCC设备":
                    w, h, orient, model = parse_spec(spec)
                    key = (model, w, h, orient, source)
                    all_dcc[source][key] += qty
                elif cat in ("检修门", "单扇防火门", "双扇防火门", "卷帘门", "铝合金门", "门", "DCC隔断/门"):
                    all_doors[source][(cat, name, spec)] += qty
                elif cat in ("混凝土柱", "钢柱", "柱", "型钢", "柱底板/垫板"):
                    all_struct[source][(cat, name, spec)] += qty
                else:
                    all_other[source].append((cat, name, spec, qty))

    # Write master table
    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        cw = csv.writer(f)

        # ── Section A: DCC Equipment ──
        cw.writerow(["一、DCC通风设备"])
        cw.writerow(["序号", "来源图纸", "轴位", "区域", "编号", "宽度/m", "高度/m", "左右接", "安装方式", "数量/台"])
        seq = 0

        # Flatten all DCC items across files, group by model+size+orient
        flat = defaultdict(int)
        for src, items in all_dcc.items():
            for (model, width, height, orient, src2), qty in items.items():
                key = (model, width, height, orient)
                flat[key] += qty  # merge across source files for subtotal

        # Sort
        sorted_items = sorted(flat.items(),
            key=lambda x: (model_sort(x[0][0]), -float(x[0][1] or 0), x[0][3] or ''))

        for (model, width, height, orient), qty in sorted_items:
            seq += 1
            # Find source files
            sources = set()
            for src, items in all_dcc.items():
                for (m, wd, ht, ori, s), q in items.items():
                    if (m, wd, ht, ori) == (model, width, height, orient):
                        sources.add(src)
            src_str = ", ".join(sorted(sources)[:3])
            if len(sources) > 3:
                src_str += f" +{len(sources)-3}"
            cw.writerow([seq, src_str, "", "", model, width, height, orient, "", qty])

        # ── Section B: DCC Racks ──
        cw.writerow([])
        cw.writerow(["二、DCC支架"])
        cw.writerow(["序号", "来源图纸", "轴位", "区域", "编号", "宽度/m", "高度/m", "左右接", "安装方式", "数量/台"])

        all_racks = defaultdict(int)
        for subdir in sorted(ALL_DIR.iterdir()):
            if not subdir.is_dir(): continue
            qty_csv = subdir / "quantity_takeoff.csv"
            if not qty_csv.exists(): continue
            with open(qty_csv, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    if row["类别"].strip() == "DCC支架":
                        spec = row["规格"].strip()
                        w, h, orient, model = parse_spec(spec)
                        qty = int(float(row["数量"].strip()))
                        key = (model, w, h, orient)
                        all_racks[key] += qty

        for (model, width, height, orient), qty in sorted(all_racks.items(),
                key=lambda x: (model_sort(x[0][0]), -float(x[0][1] or 0))):
            seq += 1
            cw.writerow([seq, "", "", "", model, width, height, orient, "", qty])

        # ── Section C: Doors ──
        cw.writerow([])
        cw.writerow(["三、门"])
        cw.writerow(["序号", "来源图纸", "类别", "名称", "规格", "数量/樘"])

        all_doors_flat = defaultdict(int)
        for src, items in all_doors.items():
            for (cat, name, spec), qty in items.items():
                all_doors_flat[(cat, name, spec)] += qty

        for (cat, name, spec), qty in sorted(all_doors_flat.items(), key=lambda x: -x[1]):
            seq += 1
            cw.writerow([seq, "", cat, name, spec, qty])

        # ── Section D: Structure ──
        cw.writerow([])
        cw.writerow(["四、结构构件"])
        cw.writerow(["序号", "来源图纸", "类别", "名称", "规格", "数量"])

        all_struct_flat = defaultdict(int)
        for src, items in all_struct.items():
            for (cat, name, spec), qty in items.items():
                all_struct_flat[(cat, name, spec)] += qty

        for (cat, name, spec), qty in sorted(all_struct_flat.items(), key=lambda x: -x[1]):
            seq += 1
            cw.writerow([seq, "", cat, name, spec, qty])

        # ── Section E: Per-file summary ──
        cw.writerow([])
        cw.writerow(["五、各图纸汇总"])
        cw.writerow(["图纸", "总实体数", "DCC设备", "DCC支架", "门", "结构", "备注"])

        for subdir in sorted(ALL_DIR.iterdir()):
            if not subdir.is_dir(): continue
            qty_csv = subdir / "quantity_takeoff.csv"
            if not qty_csv.exists(): continue
            source = subdir.name

            counts = defaultdict(int)
            with open(qty_csv, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    counts[row["类别"].strip()] += int(float(row["数量"].strip()))

            dcc_eq = counts.get("DCC设备", 0)
            dcc_rk = counts.get("DCC支架", 0)
            doors = sum(counts.get(c,0) for c in ["检修门","单扇防火门","双扇防火门","卷帘门","铝合金门","门","DCC隔断/门"])
            struct = sum(counts.get(c,0) for c in ["混凝土柱","钢柱","柱","型钢","柱底板/垫板","钢梁","梁"])

            note = ""
            if "KEQP" in source: note = "厨房设备图"
            elif "KPART" in source: note = "厨房隔断图"
            elif "AR" in source: note = "建筑图"

            cw.writerow([source, "", dcc_eq, dcc_rk, doors, struct, note])

    print(f"Master table: {OUT}")
    print(f"Total rows: {seq}")


if __name__ == "__main__":
    main()
