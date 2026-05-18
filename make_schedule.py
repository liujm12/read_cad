"""Generate equipment schedule from quantity_takeoff.csv."""
import csv, re
from pathlib import Path

QTY = Path(__file__).parent / "result" / "KEQP-v2" / "quantity_takeoff.csv"
OUT = Path(__file__).parent / "result" / "KEQP-v2" / "设备材料表.csv"


def parse_spec(spec):
    """Parse spec string into width, height, orientation, model."""
    w = h = orient = model = ""
    m = re.search(r'(\d{3,4})\s*[xX×\*]\s*(\d{3,4})', spec)
    if m:
        w = f"{int(m.group(1))/1000:.2f}"
        h = f"{int(m.group(2))/1000:.2f}"
    if '左' in spec:
        orient = '左'
    elif '右' in spec:
        orient = '右'
    m = re.search(r'(DCC-[A-Z]\*?\d?)', spec)
    if m:
        model = m.group(1)
    elif re.search(r'DCC-?\d', spec):
        m2 = re.search(r'(DCC-?\d+)', spec)
        if m2:
            model = m2.group(1)
    return w, h, orient, model


def model_sort_key(model):
    order = {'DCC-F':0,'DCC-F*2':1,'DCC-E':2,'DCC-E*2':3,'DCC-D':4,'DCC-D*2':5,
             'DCC-C':6,'DCC-C*2':7,'DCC-B':8,'DCC-B*2':9,'DCC-A':10,'DCC-A*2':11,
             'DCC-7':12,'DCC-6':13,'DCC-5':14,'DCC-J':15,'DCC-J*2':16}
    return order.get(model, 99)


def main():
    rows = []
    with open(QTY, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["类别"].strip() == "DCC设备":
                rows.append(r)

    # Parse and group
    from collections import defaultdict
    groups = defaultdict(int)
    for r in rows:
        spec = r["规格"].strip()
        w, h, orient, model = parse_spec(spec)
        qty = int(r["数量"].strip())
        key = (model, w, h, orient)
        groups[key] += qty

    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["序号", "轴位", "区域", "编号", "宽度/m", "高度/m", "左右接", "安装方式", "数量/台"])

        seq = 0
        for (model, width, height, orient), qty in sorted(groups.items(),
                key=lambda x: (model_sort_key(x[0][0]), -float(x[0][1] or 0), x[0][3] or '')):
            seq += 1
            w.writerow([seq, "", "", model, width, height, orient, "", qty])

    print(f"Generated: {OUT}")
    print(f"Rows: {seq}")

    # Verify totals
    total = sum(v for v in groups.values())
    print(f"Total DCC设备: {total}")
    for (model, w, h, orient), qty in sorted(groups.items(),
            key=lambda x: (model_sort_key(x[0][0]), -float(x[0][1] or 0), x[0][3] or '')):
        print(f"  {model:12s} {w:>5s} x {h:>5s}  {orient:2s}  → {qty:>4} 台")


if __name__ == "__main__":
    main()
