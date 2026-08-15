# -*- coding: utf-8 -*-
"""用 OR-Tools CP-SAT 给所有班排出一张周课表（星期x节次 -> 科目+教师）。"""
import json
import sys
from pathlib import Path
from collections import defaultdict

from ortools.sat.python import cp_model

from parse_input import load_classes, apply_confirmed_disambiguations, BASE
from build_atoms import build_all_atoms, validate_atom_counts, LIFE_SAFETY, PE, LABOR_AI, BANDUI, CHINESE

MATH = "数学"

# 周一上午2、3、4节禁排名单
FORBIDDEN_MON_AM234 = {
    "王国良", "毕运", "贾皓云", "黄黎黎", "晏正武", "何佳宏", "吴茜", "都培",
    "曾静", "鄢然", "唐华", "舒冬", "彭井", "吴剑锋", "王雪薇",
}

# 功能室冲突组：(教师集合, 限定年级key)
FUNCTION_ROOM_GROUPS = [
    ({"张振中", "杨清涵"}, "2024级"),
    ({"吴珍林", "缪阳", "邹旭"}, "2023级"),
    ({"吴珍林", "缪阳", "邹旭"}, "2022级"),
    ({"谢莹莹", "吴磊"}, "2021级"),
    ({"曾永佳", "李佳壕"}, "2024级"),
    ({"赵丹", "刘琳", "谢媛怡"}, "2023级"),
    ({"陈小兰", "李玉庆"}, "2022级"),
    ({"王辉", "谢媛怡"}, "2021级"),
]

GRADE26 = ("2026级", "2025级")
CAMPUS_OF_GRADE = {"2026级": "B", "2025级": "A", "2024级": "A", "2023级": "A", "2022级": "A", "2021级": "A"}

WEIGHT_DAILY_CHINESE = 1000
WEIGHT_DAILY_MATH = 1000
WEIGHT_FRIDAY_DOUBLE = 500
WEIGHT_CROSS_GRADE_ADJACENCY = 50
WEIGHT_PM_CORE_OVERFLOW = 5


def valid_slots_for_grade(grade_key):
    """返回该年级一周所有合法slot_id（day*10+period）。"""
    slots = []
    if grade_key in GRADE26:
        per_day = {1: 6, 2: 5, 3: 5, 4: 5, 5: 5}
    else:
        per_day = {1: 6, 2: 7, 3: 6, 4: 6, 5: 6}
    for day, n in per_day.items():
        for period in range(1, n + 1):
            slots.append(day * 10 + period)
    return slots


AM_PERIODS = {1, 2, 3, 4}
TUE_AM_SLOTS = {21, 22, 23, 24}
WED_AM_SLOTS = {31, 32, 33, 34}
MON_AM234_SLOTS = {12, 13, 14}
MON_P5 = 15
FRI_P34 = (53, 54)


def _pm_slots_for_day(grade_key, day):
    return {s for s in valid_slots_for_grade(grade_key) if s // 10 == day and s % 10 not in AM_PERIODS}


def build_class_key_list(classes):
    return [(c["grade_key"], c["class_no"]) for c in classes]


def _add_daily_coverage_soft(model, key, idxs, day_vars, penalty_terms, weight, max_per_day=2):
    """要求 idxs 里的atom的day尽量覆盖全部5天（软约束：每天至少1个，缺一天扣一次分）。
    有些班因为老师跨班共享+教研时间禁排导致某天物理上排不出（比如周三只有1个下午槎位，
    但共用该老师的另一个班也需要占用同一绝对时间点），这种情况让它退化，而不是让整个模型INFEASIBLE。
    """
    if not idxs:
        return
    for d in range(1, 6):
        day_bools = []
        for i in idxs:
            b = model.NewBoolVar(f"cov_{key}_{i}_d{d}")
            model.Add(day_vars[(key, i)] == d).OnlyEnforceIf(b)
            model.Add(day_vars[(key, i)] != d).OnlyEnforceIf(b.Not())
            day_bools.append(b)
        model.Add(sum(day_bools) <= max_per_day)
        covered = model.NewBoolVar(f"covered_{key}_d{d}")
        model.Add(sum(day_bools) >= 1).OnlyEnforceIf(covered)
        model.Add(sum(day_bools) == 0).OnlyEnforceIf(covered.Not())
        penalty_terms.append((weight, covered.Not(), key, d))


def candidate_slots(atom, grade_key, class_teachers_are_chinese, class_teachers_are_math):
    """根据atom的教师身份和kind，算出它可用的slot集合（域裁剪，规则1/2/5/3/4，以及"语文/数学除了
    教研当天都必须在上午"这条用户明确加强过的要求）。
    """
    base = set(valid_slots_for_grade(grade_key))

    if atom["kind"] == "banduihuodong_fixed":
        return {MON_P5}

    if atom["kind"] == "chinese_double_part":
        if grade_key in GRADE26:
            # 一二年级：优先周五3/4，退化到周一/三/四的连续两节——且退化后的连堂也必须在上午
            # （语文除了周二教研当天都要排上午，连堂当然也不例外）
            candidates = set()
            candidates.update(FRI_P34)
            for day in (1, 3, 4):
                for p in AM_PERIODS:
                    if p + 1 in AM_PERIODS:
                        candidates.add(day * 10 + p)
                        candidates.add(day * 10 + p + 1)
            candidates &= base
        else:
            candidates = set(FRI_P34) & base
        # 规则5：15人周一上午2-4节禁排，连堂退化到周一时也不能违反这条
        if any(t in FORBIDDEN_MON_AM234 for t in atom["teachers"]):
            candidates -= MON_AM234_SLOTS
        return candidates

    # 常规裁剪：规则1（语文老师周二上午不排）、规则2（数学老师周三上午不排）、规则5（15人周一上午2-4节不排）
    domain = set(base)
    teachers = atom["teachers"]
    if any(t in class_teachers_are_chinese for t in teachers):
        domain -= TUE_AM_SLOTS
    if any(t in class_teachers_are_math for t in teachers):
        if grade_key in GRADE26 and atom["subject"] == MATH:
            # 用户明确放开的口子：一二年级数学课允许部分班排在周三上午第4节（缓解同一数学老师带2个班时
            # 周三下午只有1个槎位、两个班抢不过来的问题），其余人/其余科目仍然周三上午全禁
            domain -= (WED_AM_SLOTS - {34})
        else:
            domain -= WED_AM_SLOTS
    if any(t in FORBIDDEN_MON_AM234 for t in teachers):
        domain -= MON_AM234_SLOTS

    # 语文、数学：除了各自的教研当天（语文=周二，数学=周三），其余天全部只能排上午（用户明确要求，硬约束）
    if atom["subject"] == CHINESE:
        am_only = {s for s in domain if s % 10 in AM_PERIODS}
        exception_pm = domain & _pm_slots_for_day(grade_key, 2)
        domain = am_only | exception_pm
    elif atom["subject"] == MATH:
        am_only = {s for s in domain if s % 10 in AM_PERIODS}
        exception_pm = domain & _pm_slots_for_day(grade_key, 3)
        domain = am_only | exception_pm

    return domain


def build_model(classes, all_atoms, grades_filter=None, enabled_rules=None):
    """构建CP-SAT模型。grades_filter为None时用全部81班，否则只用指定年级（用于分阶段验证）。
    enabled_rules: None表示全部规则开启；否则是一个set，只有在集合里的规则才生效（用于二分排查）。
    可选值: 'chinese_daily','math_daily','pe_bijection','no_double_subject','teacher_conflict',
           'function_room','cross_grade_adjacency'
    """
    if enabled_rules is None:
        enabled_rules = {"chinese_daily", "math_daily", "pe_bijection", "no_double_subject",
                          "teacher_conflict", "function_room", "cross_grade_adjacency"}
    model = cp_model.CpModel()

    if grades_filter is not None:
        classes = [c for c in classes if c["grade_key"] in grades_filter]

    class_keys = build_class_key_list(classes)
    class_by_key = {(c["grade_key"], c["class_no"]): c for c in classes}

    # 每个班：识别"语文老师"集合、"数学老师"集合（用于教研时间裁剪）
    chinese_teachers_by_class = {}
    math_teachers_by_class = {}
    for c in classes:
        key = (c["grade_key"], c["class_no"])
        atoms = all_atoms[key]
        chi = set()
        mat = set()
        for a in atoms:
            if a["subject"] == CHINESE or a.get("double_role"):
                chi.update(a["teachers"])
            if a["subject"] == MATH:
                mat.update(a["teachers"])
        chinese_teachers_by_class[key] = chi
        math_teachers_by_class[key] = mat

    slot_vars = {}  # (class_key, atom_idx) -> IntVar
    day_vars = {}
    per_vars = {}
    penalty_terms = []  # list of (weight, bool_var, key, day) —— 1表示违反了一次软约束

    for key in class_keys:
        c = class_by_key[key]
        grade_key = key[0]
        atoms = all_atoms[key]
        class_slot_vars = []
        for idx, atom in enumerate(atoms):
            dom = candidate_slots(atom, grade_key, chinese_teachers_by_class[key], math_teachers_by_class[key])
            if not dom:
                raise ValueError(f"班{key}的第{idx}个atom {atom} 域裁剪后为空，规则冲突，无法建模")
            dom_sorted = sorted(dom)
            v = model.NewIntVarFromDomain(
                cp_model.Domain.FromValues(dom_sorted), f"slot_{key}_{idx}"
            )
            slot_vars[(key, idx)] = v
            class_slot_vars.append(v)

            day_v = model.NewIntVar(1, 5, f"day_{key}_{idx}")
            per_v = model.NewIntVar(1, 7, f"per_{key}_{idx}")
            model.AddDivisionEquality(day_v, v, 10)
            model.AddModuloEquality(per_v, v, 10)
            day_vars[(key, idx)] = day_v
            per_vars[(key, idx)] = per_v

        # 同一个班的所有atom必须落在不同的slot（一节课只能上一门）
        model.AddAllDifferent(class_slot_vars)

        # 周五连堂：两节挨着（第二节=第一节+1）
        first_idx = next((i for i, a in enumerate(atoms) if a.get("double_role") == "first"), None)
        second_idx = next((i for i, a in enumerate(atoms) if a.get("double_role") == "second"), None)
        if first_idx is not None and second_idx is not None:
            model.Add(slot_vars[(key, second_idx)] == slot_vars[(key, first_idx)] + 1)
            if grade_key in GRADE26:
                # 一二年级：硬约束只保证"某天连堂"，软偏好"优先周五"（三到六年级本身域就只有周五，不需要这条）
                is_friday = model.NewBoolVar(f"fri_{key}")
                model.Add(slot_vars[(key, first_idx)] == FRI_P34[0]).OnlyEnforceIf(is_friday)
                model.Add(slot_vars[(key, first_idx)] != FRI_P34[0]).OnlyEnforceIf(is_friday.Not())
                penalty_terms.append((WEIGHT_FRIDAY_DOUBLE, is_friday.Not(), key, "friday_double"))

        # 语文/数学/外语尽量排上午，下午最多1节（规则10，软约束）
        core_idxs = [i for i, a in enumerate(atoms) if a["subject"] in (CHINESE, MATH, "外语")]
        for d in range(1, 6):
            pm_bools = []
            for i in core_idxs:
                b = model.NewBoolVar(f"pm_{key}_{i}_d{d}")
                is_this_day = model.NewBoolVar(f"day_{key}_{i}_eq{d}")
                model.Add(day_vars[(key, i)] == d).OnlyEnforceIf(is_this_day)
                model.Add(day_vars[(key, i)] != d).OnlyEnforceIf(is_this_day.Not())
                is_pm = model.NewBoolVar(f"ispm_{key}_{i}")
                model.Add(per_vars[(key, i)] >= 5).OnlyEnforceIf(is_pm)
                model.Add(per_vars[(key, i)] <= 4).OnlyEnforceIf(is_pm.Not())
                model.AddBoolAnd([is_this_day, is_pm]).OnlyEnforceIf(b)
                model.AddBoolOr([is_this_day.Not(), is_pm.Not()]).OnlyEnforceIf(b.Not())
                pm_bools.append(b)
            if pm_bools:
                excess = model.NewIntVar(0, len(pm_bools), f"pmexcess_{key}_d{d}")
                model.Add(excess >= sum(pm_bools) - 1)
                penalty_terms.append((WEIGHT_PM_CORE_OVERFLOW, excess, key, f"pm_overflow_d{d}"))

        # 每天都要有语文课（规则7）：语文类atom(含连堂两节)的day尽量覆盖5天（软约束，见_add_daily_coverage_soft注释）
        if "chinese_daily" in enabled_rules:
            chinese_idxs = [i for i, a in enumerate(atoms) if a["subject"] == CHINESE]
            _add_daily_coverage_soft(model, key, chinese_idxs, day_vars, penalty_terms, WEIGHT_DAILY_CHINESE)

        # 每天都要有数学老师上的课（规则7，综合实践算数学老师的课，含combo2里那半是"综合实践"且由数学老师教的情况——
        # 六年级的综合实践0.5被合并进了劳动/劳动人工智能combo，那个slot单双周里有一半也是数学老师在教，同样算数学课）
        if "math_daily" in enabled_rules:
            def _is_math_atom(a):
                if a["subject"] == MATH:
                    return True
                if a["subject"] == "综合实践" and set(a["teachers"]) & math_teachers_by_class[key]:
                    return True
                if a["kind"] == "combo" and a["subject"].split("/")[0] == "综合实践" \
                        and a["teachers"][0] in math_teachers_by_class[key]:
                    return True
                return False
            math_idxs = [i for i, a in enumerate(atoms) if _is_math_atom(a)]
            _add_daily_coverage_soft(model, key, math_idxs, day_vars, penalty_terms, WEIGHT_DAILY_MATH)

        # 体育每天最多1节、且4.5节里的0.5(combo)恰好补齐第5天 -> PE相关atom(4个整节+1个combo)的day两两不同
        if "pe_bijection" in enabled_rules:
            pe_idxs = [i for i, a in enumerate(atoms)
                       if a["subject"] == PE or a.get("combo_flag") == "life_pe"]
            if len(pe_idxs) > 1:
                model.AddAllDifferent([day_vars[(key, i)] for i in pe_idxs])

        # 外语/音乐/美术/科学/道德与法治：同一天不能出现2节（规则8）
        if "no_double_subject" in enabled_rules:
            for subj in ("外语", "音乐", "美术", "科学", "道德与法治"):
                idxs = [i for i, a in enumerate(atoms) if a["subject"] == subj]
                if len(idxs) > 1:
                    model.AddAllDifferent([day_vars[(key, i)] for i in idxs])

    # 教师池：全局按老师姓名收集他/她所有的(班,atom)——教师冲突约束和跨年级错开约束都要用
    teacher_pool = defaultdict(list)
    for key in class_keys:
        atoms = all_atoms[key]
        for idx, atom in enumerate(atoms):
            for t in atom["teachers"]:
                teacher_pool[t].append((key, idx))

    # 教师冲突：全局同一老师不能同一时间出现在两个班（含combo的两位老师都算占用）
    if "teacher_conflict" in enabled_rules:
        for t, pool in teacher_pool.items():
            if len(pool) > 1:
                model.AddAllDifferent([slot_vars[p] for p in pool])

    # 规则11：同一位老师跨年级的课尽量错开，不要前后两节连堂——
    # 跨校区（一年级B校区 vs 其余A校区）物理上不可能连着上，升级为硬约束直接禁止；
    # 同校区跨年级仅为软偏好，用惩罚项尽量避免。
    if "cross_grade_adjacency" in enabled_rules:
        pair_count = 0
        for t, pool in teacher_pool.items():
            by_grade = defaultdict(list)
            for key, idx in pool:
                by_grade[key[0]].append((key, idx))
            grades_here = sorted(by_grade)
            for gi in range(len(grades_here)):
                for gj in range(gi + 1, len(grades_here)):
                    g1, g2 = grades_here[gi], grades_here[gj]
                    is_cross_campus = CAMPUS_OF_GRADE[g1] != CAMPUS_OF_GRADE[g2]
                    for key1, idx1 in by_grade[g1]:
                        for key2, idx2 in by_grade[g2]:
                            pair_count += 1
                            diff = model.NewIntVar(-6, 6, f"perdiff_{t}_{key1}_{idx1}_{key2}_{idx2}")
                            model.Add(diff == per_vars[(key1, idx1)] - per_vars[(key2, idx2)])
                            absdiff = model.NewIntVar(0, 6, f"absperdiff_{t}_{key1}_{idx1}_{key2}_{idx2}")
                            model.AddAbsEquality(absdiff, diff)
                            is_adjacent = model.NewBoolVar(f"adj_{t}_{key1}_{idx1}_{key2}_{idx2}")
                            model.Add(absdiff == 1).OnlyEnforceIf(is_adjacent)
                            model.Add(absdiff != 1).OnlyEnforceIf(is_adjacent.Not())
                            same_day = model.NewBoolVar(f"sameday_{t}_{key1}_{idx1}_{key2}_{idx2}")
                            model.Add(day_vars[(key1, idx1)] == day_vars[(key2, idx2)]).OnlyEnforceIf(same_day)
                            model.Add(day_vars[(key1, idx1)] != day_vars[(key2, idx2)]).OnlyEnforceIf(same_day.Not())
                            if is_cross_campus:
                                # 硬约束：不允许同一天相邻节次（校区之间没法在课间赶过去）
                                model.AddBoolOr([same_day.Not(), is_adjacent.Not()])
                            else:
                                conflict = model.NewBoolVar(f"conflict_{t}_{key1}_{idx1}_{key2}_{idx2}")
                                model.AddBoolAnd([same_day, is_adjacent]).OnlyEnforceIf(conflict)
                                model.AddBoolOr([same_day.Not(), is_adjacent.Not()]).OnlyEnforceIf(conflict.Not())
                                penalty_terms.append((WEIGHT_CROSS_GRADE_ADJACENCY, conflict, t,
                                                       f"cross_grade_adj_{g1}_{g2}"))

    # 功能室冲突组（按年级限定）
    if "function_room" in enabled_rules:
        for teachers, grade_key in FUNCTION_ROOM_GROUPS:
            if grades_filter is not None and grade_key not in grades_filter:
                continue
            pool = []
            for key in class_keys:
                if key[0] != grade_key:
                    continue
                for idx, atom in enumerate(all_atoms[key]):
                    if any(t in teachers for t in atom["teachers"]):
                        pool.append((key, idx))
            if len(pool) > 1:
                model.AddAllDifferent([slot_vars[p] for p in pool])

    if penalty_terms:
        model.Minimize(sum(weight * term for weight, term, _, _ in penalty_terms))

    return model, slot_vars, day_vars, per_vars, class_keys, penalty_terms


def apply_hint(model, slot_vars, hint_solution):
    """用上一轮的解当warm start，帮CP-SAT更快收敛到更优解（而不是从零开始搜）。"""
    count = 0
    for (key, idx), v in slot_vars.items():
        if key in hint_solution and idx in hint_solution[key]:
            model.AddHint(v, hint_solution[key][idx])
            count += 1
    return count


def solve_and_report(classes, all_atoms, grades_filter=None, time_limit=120, enabled_rules=None, hint_solution=None):
    model, slot_vars, day_vars, per_vars, class_keys, penalty_terms = build_model(
        classes, all_atoms, grades_filter, enabled_rules)
    if hint_solution:
        apply_hint(model, slot_vars, hint_solution)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_search_workers = 12
    status = solver.Solve(model)
    status_name = solver.StatusName(status)
    print(f"求解状态: {status_name}")
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        solution = {}
        for (key, idx), v in slot_vars.items():
            solution.setdefault(key, {})[idx] = solver.Value(v)
        violations = []
        for weight, term, key, tag in penalty_terms:
            val = solver.Value(term)
            if val:
                violations.append((key, tag, val, weight))
        return status_name, solution, violations
    return status_name, None, []


if __name__ == "__main__":
    classes = load_classes()
    apply_confirmed_disambiguations(classes)
    all_atoms, warnings = build_all_atoms(classes)
    if warnings:
        print(f"注意：atoms生成有{len(warnings)}条警告，先看build_atoms.py输出")

    grades_arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    time_limit = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    use_hint = len(sys.argv) > 3 and sys.argv[3] == "--hint"
    if grades_arg == "all":
        test_grades = None
    else:
        test_grades = set(grades_arg.split(","))

    hint_solution = None
    if use_hint:
        sol_path = BASE / "_solution.json"
        if sol_path.exists():
            with open(sol_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            hint_solution = {}
            for k, v in raw.items():
                g, n = k.split("|")
                hint_solution[(g, int(n))] = {int(i): slot for i, slot in v.items()}
            print(f"已加载上一轮解作为warm start hint（{len(hint_solution)}个班）")

    print(f"=== 对 {test_grades or '全部81班'} 求解（时限{time_limit}秒{'，带hint' if hint_solution else ''}） ===")
    status_name, solution, violations = solve_and_report(
        classes, all_atoms, grades_filter=test_grades, time_limit=time_limit, hint_solution=hint_solution)
    if violations:
        print(f"\n软约束违反（共{len(violations)}处）：")
        for key, tag, val, weight in violations:
            print(f"  {key} {tag}: {val}（权重{weight}）")
    else:
        print("\n无软约束违反")

    if solution:
        out_path = BASE / "_solution.json"
        serializable = {f"{g}|{n}": slots for (g, n), slots in solution.items()}
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
        print(f"\n已写出 {out_path}")
