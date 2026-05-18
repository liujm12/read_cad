# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

DWG 图纸分析工具。读取 AutoCAD DWG 文件，自动提取实体坐标、生成工程量清单 (BOQ) 和分析报告。

## 前置条件

- Python 3.13+ (Anaconda)
- `ezdxf` 库 (`pip install ezdxf`)
- ODA File Converter 安装在默认路径 `C:\Program Files\ODA\ODAFileConverter\`

## 核心命令

```bash
# 单文件分析
python dwg_analyzer.py cad/图纸.dwg

# 批量分析整个目录
python dwg_analyzer.py cad/

# 跳过逐层 CSV（仅出工程量+报告，更快）
python dwg_analyzer.py cad/ --skip-csv

# 指定输出目录
python dwg_analyzer.py cad/图纸.dwg -o result/custom_name
```

## 输出结构

每个 DWG 在 `result/<图纸名>/` 下生成：
- `analysis_report.md` — 完整分析报告（基本信息、图层、实体统计、工程量）
- `quantity_takeoff.csv` — 工程量清单（可直接 Excel 打开）
- `reconciliation_report.txt` — 核对报告（每个图层：计入 vs 排除，验证总数吻合）
- `all_entities.csv` — 全部实体坐标（`--skip-csv` 跳过）
- `layer_*.csv` — 各图层实体分别导出（`--skip-csv` 跳过）

## 核对验证

每次分析后检查 `reconciliation_report.txt` 确保：
- 总实体数 = 已计入 + 排除 → 吻合
- 排除项全部为标注/填充/云线等非构件（无可疑 INSERT 被误排除）
- 外部参照已标注来源

## 全量分析

对多文件做统一分析时，使用 `full_analysis.py`:

```bash
python full_analysis.py
# 自动加载 cad/ 下所有 DWG → 统一轴网 → 统一分析 → 输出到 result/all/
```

输出: `result/all/设备材料表_vN.csv` + `install_schedule.csv`

## OBSERVATIONS.md

图纸命名规律、图层体系、型号识别策略等经验记录在 `OBSERVATIONS.md`。
该文件为观察记录（非指令），验证后移入本文件。
