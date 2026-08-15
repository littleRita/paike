# -*- coding: utf-8 -*-
"""对求解结果做全面校验，产出文字报告（节数、教师冲突、规则满足情况、退化清单）。"""
import json
from collections import defaultdict

from parse_input import load_classes, apply_confirmed_disambiguations, BASE
from build_atoms import build_all_atoms, CHINESE, PE, LIFE_SAFETY, LABOR_AI, BANDUI
from solve import (
    FORBIDDEN_MON_AM234, FUNCTION_ROOM_GROUPS, MATH, GRADE26,
    TUE_AM_SLOTS, WED_AM_SLOTS, MON_AM234_SLOTS, MON_P5, FRI_P34,
    CAMPUS_OF_GRADE, AM_PERIODS,
)


def load_solution():
    sol_path = BASE / "_solution.json"
    with open(sol_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    solution = {}
    for k, v in raw.items():
        g, n = k.split("|")
        solution[(g, int(n))] = {int(i): slot for i, slot in v.items()}
    return solution


def validate(classes, all_atoms, solution):
    report = {"errors": [], "degradations": [], "ok_summary": []}
    class_by_key = {(c["grade_key"], c["class_no"]): c for c in classes}

    solved_keys = set(solution.keys())
    all_keys = set(class_by_key.keys())
    missing = all_keys - solved_keys
    if missing:
        report["errors"].append(f"{len(missing)}个班没有求解结果: {sorted(missing)}")

    # 1. 每班节数校验（同build_atoms的validate_atom_counts，但基于实际填入的slot数）
    for key in solved_keys:
        expected = 26 if key[0] in GRADE26 else 31
        actual = len(solution[key])
        if actual != expected:
            report["errors"].append(f"{key}：期望{expected}节，实际排入{actual}节")

    # 2. 教师冲突：同一教师同一slot出现在>1个(class,atom)
    slot_by_teacher = defaultdict(list)
    for key in solved_keys:
        atoms = all_atoms[key]
        for idx, slot in solution[key].items():
            for t in atoms[idx]["teachers"]:
                slot_by_teacher[(t, slot)].append((key, idx))
    conflicts = {k: v for k, v in slot_by_teacher.items() if len(v) > 1}
    if conflicts:
        for (t, slot), locs in conflicts.items():
            day, period = slot // 10, slot % 10
            report["errors"].append(f"教师冲突: {t} 在周{day}第{period}节同时出现在 {locs}")
    else:
        report["ok_summary"].append("教师冲突: 0处（含combo双教师占用）")

    # 3. 规则3：班队活动固定周一第5节
    bandui_ok = True
    for key in solved_keys:
        atoms = all_atoms[key]
        for idx, slot in solution[key].items():
            if atoms[idx]["kind"] == "banduihuodong_fixed" and slot != MON_P5:
                report["errors"].append(f"{key}: 班队活动没有排在周一第5节（实际slot={slot}）")
                bandui_ok = False
    if bandui_ok:
        report["ok_summary"].append("规则3（班队活动固定周一第5节）: 全部满足")

    # 4. 规则1/2：语文老师周二上午不排、数学老师周三上午不排——
    # 用户明确放开的例外：一二年级"数学"科目允许排在周三上午第4节(slot末位=4)，缓解同一数学老师带2个班的冲突
    rule12_violations = []
    for key in solved_keys:
        atoms = all_atoms[key]
        chinese_teachers = set()
        math_teachers = set()
        for a in atoms:
            if a["subject"] == CHINESE or a.get("double_role"):
                chinese_teachers.update(a["teachers"])
            if a["subject"] == MATH:
                math_teachers.update(a["teachers"])
        for idx, slot in solution[key].items():
            a = atoms[idx]
            teachers = a["teachers"]
            if slot in TUE_AM_SLOTS and any(t in chinese_teachers for t in teachers):
                rule12_violations.append(f"{key} atom{idx} 语文老师{teachers}被排在周二上午(slot={slot})")
            if slot in WED_AM_SLOTS and any(t in math_teachers for t in teachers):
                is_allowed_exception = (key[0] in GRADE26 and a["subject"] == MATH and slot % 10 == 4)
                if not is_allowed_exception:
                    rule12_violations.append(f"{key} atom{idx} 数学老师{teachers}被排在周三上午(slot={slot})")
    if rule12_violations:
        report["errors"].extend(rule12_violations)
    else:
        report["ok_summary"].append("规则1/2（教研时间禁排，含一二年级数学周三第4节的用户放开例外）: 全部满足")

    # 5. 规则5：15人周一上午2-4节禁排
    rule5_violations = []
    for key in solved_keys:
        atoms = all_atoms[key]
        for idx, slot in solution[key].items():
            if slot in MON_AM234_SLOTS and any(t in FORBIDDEN_MON_AM234 for t in atoms[idx]["teachers"]):
                rule5_violations.append(f"{key} atom{idx} {atoms[idx]['teachers']} 被排在周一上午2-4节(slot={slot})")
    if rule5_violations:
        report["errors"].extend(rule5_violations)
    else:
        report["ok_summary"].append("规则5（15人周一上午2-4节禁排）: 全部满足")

    # 6. 规则4：功能室冲突组同一节课检查
    rule4_violations = []
    for teachers, grade_key in FUNCTION_ROOM_GROUPS:
        slot_map = defaultdict(list)
        for key in solved_keys:
            if key[0] != grade_key:
                continue
            atoms = all_atoms[key]
            for idx, slot in solution[key].items():
                hit = set(atoms[idx]["teachers"]) & teachers
                if hit:
                    slot_map[slot].append((key, idx, hit))
        for slot, entries in slot_map.items():
            names_involved = set()
            for _, _, hit in entries:
                names_involved |= hit
            if len(entries) > 1 and len(names_involved) > 1:
                rule4_violations.append(f"功能室冲突组{teachers}({grade_key})在slot={slot}同时出现: {entries}")
    if rule4_violations:
        report["errors"].extend(rule4_violations)
    else:
        report["ok_summary"].append("规则4（功能室冲突组）: 全部满足")

    # 7. 规则2/一二年级周五连堂：检查chinese_double_part是否落在周五3/4，否则记为"退化"
    for key in solved_keys:
        atoms = all_atoms[key]
        first_idx = next((i for i, a in enumerate(atoms) if a.get("double_role") == "first"), None)
        if first_idx is None:
            continue
        slot = solution[key][first_idx]
        if slot != FRI_P34[0]:
            day, period = slot // 10, slot % 10
            grade_type = "三到六年级" if key[0] not in GRADE26 else "一二年级"
            severity = "errors" if key[0] not in GRADE26 else "degradations"
            report[severity].append(
                f"{key}（{grade_type}）：语文连堂没有排在周五3/4节，实际排在周{day}第{period}/{period+1}节"
            )

    # 8. 每天语文/数学覆盖情况（软约束，逐班列出缺口）
    for key in solved_keys:
        atoms = all_atoms[key]
        days_with_chinese = set()
        days_with_math = set()
        math_teachers = set(t for a in atoms if a["subject"] == MATH for t in a["teachers"])
        for idx, slot in solution[key].items():
            day = slot // 10
            a = atoms[idx]
            if a["subject"] == CHINESE:
                days_with_chinese.add(day)
            if a["subject"] == MATH or (a["subject"] == "综合实践" and set(a["teachers"]) & math_teachers):
                days_with_math.add(day)
            if a["kind"] == "combo" and a["subject"].split("/")[0] == "综合实践" and a["teachers"][0] in math_teachers:
                days_with_math.add(day)
        missing_chinese_days = {1, 2, 3, 4, 5} - days_with_chinese
        missing_math_days = {1, 2, 3, 4, 5} - days_with_math
        if missing_chinese_days:
            report["degradations"].append(f"{key}：周{sorted(missing_chinese_days)}没有语文课")
        if missing_math_days:
            report["degradations"].append(f"{key}：周{sorted(missing_math_days)}没有数学老师上的课")

    # 9. 语文/数学：除了教研当天(语文=周二,数学=周三)，其余天必须在上午（用户要求，硬约束校验）
    am_violations = []
    for key in solved_keys:
        atoms = all_atoms[key]
        for idx, slot in solution[key].items():
            a = atoms[idx]
            day, period = slot // 10, slot % 10
            if a["subject"] == CHINESE and period not in AM_PERIODS and day != 2:
                am_violations.append(f"{key}: 语文被排在周{day}第{period}节(下午)，非周二教研日")
            if a["subject"] == MATH and period not in AM_PERIODS and day != 3:
                am_violations.append(f"{key}: 数学被排在周{day}第{period}节(下午)，非周三教研日")
    if am_violations:
        report["errors"].extend(am_violations)
    else:
        report["ok_summary"].append("语文/数学除教研当天必须上午: 全部满足")

    # 10. 规则11：同一老师跨年级/跨校区相邻节次——跨校区必须为0（硬约束），同校区跨年级统计退化条数
    cross_campus_violations = []
    cross_grade_soft_count = 0
    teacher_atom_slots = defaultdict(list)
    for key in solved_keys:
        atoms = all_atoms[key]
        for idx, slot in solution[key].items():
            for t in atoms[idx]["teachers"]:
                teacher_atom_slots[t].append((key, slot))
    for t, entries in teacher_atom_slots.items():
        by_grade = defaultdict(list)
        for key, slot in entries:
            by_grade[key[0]].append(slot)
        grades_here = sorted(by_grade)
        for i in range(len(grades_here)):
            for j in range(i + 1, len(grades_here)):
                g1, g2 = grades_here[i], grades_here[j]
                for s1 in by_grade[g1]:
                    for s2 in by_grade[g2]:
                        if s1 // 10 == s2 // 10 and abs(s1 % 10 - s2 % 10) == 1:
                            if CAMPUS_OF_GRADE[g1] != CAMPUS_OF_GRADE[g2]:
                                cross_campus_violations.append(f"{t}: {g1}与{g2}(跨校区)在同一天相邻节次")
                            else:
                                cross_grade_soft_count += 1
    if cross_campus_violations:
        report["errors"].extend(cross_campus_violations)
    else:
        report["ok_summary"].append("规则11跨校区相邻(硬约束): 全部满足")
    if cross_grade_soft_count:
        report["degradations"].append(f"规则11同校区跨年级相邻(软约束)：共{cross_grade_soft_count}处未完全错开")
    else:
        report["ok_summary"].append("规则11同校区跨年级相邻(软约束): 全部满足")

    return report


def print_report(report):
    print(f"=== 硬性错误（{len(report['errors'])}条） ===")
    for e in report["errors"]:
        print(" -", e)
    print(f"\n=== 软约束退化（{len(report['degradations'])}条） ===")
    for d in report["degradations"]:
        print(" -", d)
    print(f"\n=== 完全满足的规则 ===")
    for s in report["ok_summary"]:
        print(" -", s)


if __name__ == "__main__":
    classes = load_classes()
    apply_confirmed_disambiguations(classes)
    all_atoms, _ = build_all_atoms(classes)
    solution = load_solution()
    report = validate(classes, all_atoms, solution)
    print_report(report)
