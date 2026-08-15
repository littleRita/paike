# -*- coding: utf-8 -*-
"""把 parse_input.load_classes() 产出的"科目+节数"数据，展开成排课求解器用的原子课时列表（atoms）。

每个 atom = 一个不可再分的课时槎位需求：
  - kind: normal / chinese_double_part / banduihuodong_fixed / combo
  - subject: 显示用科目名（combo 是 "A/B" 形式）
  - teachers: [teacher_name, ...]（combo 有2个，其余1个）
  - periods: 恒为1（半节课已经在这一层合并成整节）
"""
import json
from pathlib import Path
from collections import defaultdict

from parse_input import load_classes, apply_confirmed_disambiguations, BASE

LIFE_SAFETY = "生命.生态.安全&心理健康"
PE = "体育与健康"
LABOR_AI = "劳动&人工智能"
BANDUI = "班队活动&生命.生态.安全"
CHINESE = "语文"


def _pop_entry(entries, predicate):
    for i, e in enumerate(entries):
        if predicate(e):
            return entries.pop(i)
    return None


def build_atoms_for_class(c):
    entries = [dict(e) for e in c["subjects"]]  # 浅拷贝，可安全弹出
    atoms = []
    warnings = []

    # ---- combo 1: 生命.生态.安全&心理健康(0.5) + 体育与健康 的 0.5 零头 ----
    e_life = _pop_entry(entries, lambda e: e["subject"] == LIFE_SAFETY)
    e_pe = _pop_entry(entries, lambda e: e["subject"] == PE)
    if e_life is None or e_pe is None:
        warnings.append(f"{c['grade_name']}{c['class_no']}班：缺少 {LIFE_SAFETY} 或 {PE} 列，无法组合combo1")
    else:
        if abs(e_life["periods"] - 0.5) > 1e-6:
            warnings.append(f"{c['grade_name']}{c['class_no']}班：{LIFE_SAFETY} 节数不是0.5（实际{e_life['periods']}）")
        pe_total = e_pe["periods"]
        pe_whole = int(pe_total - 0.5) if abs(pe_total - int(pe_total) - 0.5) < 1e-6 else int(pe_total)
        if abs(pe_total - (pe_whole + 0.5)) > 1e-6:
            warnings.append(f"{c['grade_name']}{c['class_no']}班：{PE} 节数{pe_total}不是"
                             f"'整数+0.5'的形式，无法拆出0.5零头给combo1")
            pe_whole = int(round(pe_total))
        else:
            atoms.append({
                "kind": "combo", "subject": f"{LIFE_SAFETY}/{PE}",
                "teachers": [e_life["teacher_raw"], e_pe["teacher_raw"]],
                "combo_flag": "life_pe",
            })
        for _ in range(pe_whole):
            atoms.append({"kind": "normal", "subject": PE, "teachers": [e_pe["teacher_raw"]]})

    # ---- combo 2: 劳动&人工智能(0.5) + 该年级另一个0.5零头（动态识别，不硬编码） ----
    e_ai = _pop_entry(entries, lambda e: e["subject"] == LABOR_AI)
    e_other_half = _pop_entry(entries, lambda e: abs(e["periods"] - 0.5) < 1e-6)
    if e_ai is None:
        warnings.append(f"{c['grade_name']}{c['class_no']}班：缺少 {LABOR_AI} 列，无法组合combo2")
    elif e_other_half is None:
        warnings.append(f"{c['grade_name']}{c['class_no']}班：找不到与 {LABOR_AI} 配对的0.5零头科目")
    else:
        atoms.append({
            "kind": "combo", "subject": f"{e_other_half['subject']}/{LABOR_AI}",
            "teachers": [e_other_half["teacher_raw"], e_ai["teacher_raw"]],
            "combo_flag": "labor_ai",
        })

    # ---- 班队活动&生命.生态.安全：固定周一第5节，教师默认=语文老师，除非班主任列有override标注 ----
    e_bandui = _pop_entry(entries, lambda e: e["subject"] == BANDUI)
    if e_bandui is None:
        warnings.append(f"{c['grade_name']}{c['class_no']}班：缺少 {BANDUI} 列")
    else:
        teacher = e_bandui["teacher_raw"]
        if c["homeroom_flags"].get("banduihuodong_override") and c["homeroom_raw"]:
            teacher = c["homeroom_raw"]
        atoms.append({"kind": "banduihuodong_fixed", "subject": BANDUI, "teachers": [teacher]})

    # ---- 语文：拆出2节做"周五连堂"（或退化到周一/三/四），剩下的是散节 ----
    e_chinese = _pop_entry(entries, lambda e: e["subject"] == CHINESE)
    if e_chinese is None:
        warnings.append(f"{c['grade_name']}{c['class_no']}班：缺少 {CHINESE} 列")
    else:
        total = e_chinese["periods"]
        if total < 2:
            warnings.append(f"{c['grade_name']}{c['class_no']}班：{CHINESE} 只有{total}节，不够组成连堂")
            for _ in range(int(total)):
                atoms.append({"kind": "normal", "subject": CHINESE, "teachers": [e_chinese["teacher_raw"]]})
        else:
            atoms.append({"kind": "chinese_double_part", "subject": CHINESE,
                           "teachers": [e_chinese["teacher_raw"]], "double_role": "first"})
            atoms.append({"kind": "chinese_double_part", "subject": CHINESE,
                           "teachers": [e_chinese["teacher_raw"]], "double_role": "second"})
            for _ in range(int(total) - 2):
                atoms.append({"kind": "normal", "subject": CHINESE, "teachers": [e_chinese["teacher_raw"]]})

    # ---- 剩余所有科目：应为整数节次，逐一展开成散节 ----
    for e in entries:
        periods = e["periods"]
        if abs(periods - round(periods)) > 1e-6:
            warnings.append(f"{c['grade_name']}{c['class_no']}班：{e['subject']} 节数{periods}不是整数，"
                             f"未纳入已知combo规则，按四舍五入处理")
        for _ in range(int(round(periods))):
            atoms.append({"kind": "normal", "subject": e["subject"], "teachers": [e["teacher_raw"]]})

    return atoms, warnings


def build_all_atoms(classes):
    all_atoms = {}
    all_warnings = []
    for c in classes:
        key = (c["grade_key"], c["class_no"])
        atoms, warnings = build_atoms_for_class(c)
        all_atoms[key] = atoms
        all_warnings.extend(warnings)
    return all_atoms, all_warnings


def validate_atom_counts(classes, all_atoms):
    problems = []
    for c in classes:
        key = (c["grade_key"], c["class_no"])
        expected = 26 if c["grade_key"] in ("2026级", "2025级") else 31
        actual = len(all_atoms[key])
        if actual != expected:
            problems.append(f"{c['grade_name']}{c['class_no']}班：期望{expected}个课时槎位，实际生成{actual}个")
    return problems


if __name__ == "__main__":
    classes = load_classes()
    apply_confirmed_disambiguations(classes)
    all_atoms, warnings = build_all_atoms(classes)

    print(f"共{len(all_atoms)}个班生成atoms")
    if warnings:
        print(f"\n=== 生成过程警告（{len(warnings)}条）===")
        for w in warnings:
            print(" -", w)
    else:
        print("生成过程无警告")

    count_problems = validate_atom_counts(classes, all_atoms)
    if count_problems:
        print(f"\n=== 槎位数量校验问题（{len(count_problems)}条）===")
        for p in count_problems:
            print(" -", p)
    else:
        print("\n所有班槎位数量校验通过（=26或31）")

    # 抽样打印一个班的atoms看结构
    sample_key = ("2026级", 2)
    print(f"\n=== 抽样：{sample_key} 的atoms ===")
    for a in all_atoms.get(sample_key, []):
        print(" -", a)

    out_path = BASE / "_atoms.json"
    serializable = {f"{g}|{n}": atoms for (g, n), atoms in all_atoms.items()}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)
    print(f"\n已写出 {out_path}")
