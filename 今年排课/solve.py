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
# 注意：信息科技/劳动&人工智能 这一条线的分组已按用户2026-08最新更正版本重排（旧版本有误：
# 旧版把"吴珍林、缪阳、邹旭"挂到五年级、"谢莹莹、吴磊"挂到六年级，与实际任课年级不符，
# 导致这两条约束实际上是空的/无效的）。其余（科学、美术等）功能室分组保持不变。
FUNCTION_ROOM_GROUPS = [
    # —— 信息科技 / 劳动&人工智能 条线（2026-08 更正版）——
    ({"张振中", "杨清涵"}, "2024级"),          # 三年级
    ({"吴珍林", "缪阳", "邹旭"}, "2023级"),      # 四年级
    ({"谢莹莹", "吴磊"}, "2022级"),            # 五年级
    ({"吴磊", "李宏伟"}, "2021级"),            # 六年级
    # —— 其余功能室分组（未变更）——
    ({"曾永佳", "李佳壕"}, "2024级"),
    ({"赵丹", "刘琳", "谢媛怡"}, "2023级"),
    ({"陈小兰", "李玉庆"}, "2022级"),
    ({"王辉", "谢媛怡"}, "2021级"),
]

# 点名指定"除某一天外，其余时段必须排上午"的老师：教师姓名 -> 允许下午的那一天(1=周一..5=周五)
# 用户要求：卢诗雨的课除了周二，其他时段都排在上午
TEACHER_AM_ONLY_EXCEPT_DAY = {
    "卢诗雨": 2,
}

# 单双周合并槎的显示约定：combo的第一个科目=单周，第二个科目=双周
WEEK_LABEL_FIRST = "单周"
WEEK_LABEL_SECOND = "双周"

GRADE26 = ("2026级", "2025级")
CAMPUS_OF_GRADE = {"2026级": "B", "2025级": "A", "2024级": "A", "2023级": "A", "2022级": "A", "2021级": "A"}

WEIGHT_DAILY_CHINESE = 20000
WEIGHT_DAILY_MATH = 50000
WEIGHT_FRIDAY_DOUBLE = 500
WEIGHT_MATH_PM_OVERFLOW = 300
WEIGHT_CROSS_GRADE_ADJACENCY = 50
WEIGHT_PM_CORE_OVERFLOW = 5

# 需求1：同一老师连续排课上限（按老师本人算，语文老师连带道法/劳动/班队活动，数学老师连带综合实践/劳动）
# 语文≤2节；数学≤3节；其余所有专职学科一律≤3节且每天≤5节（用户确认：生命生态安全&心理健康、
# 劳动&人工智能与音乐/美术同档；习读本、道德与法治等其余专职学科同理）
RUN_CAP = {
    "语文": 2,
    "数学": 3,
    "外语": 3, "音乐": 3, "体育与健康": 3, "美术": 3, "科学": 3,
    "信息科技": 3, "劳动&人工智能": 3, "生命.生态.安全&心理健康": 3,
    "习读本": 3, "道德与法治": 3, "综合实践": 3, "劳动": 3, "阅读": 3, "国际理解": 3,
}
# 需求1：专职老师（语文、数学之外）每天最多5节课；语文老师另有更严的每天≤3节（用户明确要求，
# 哪怕这3节不是连续的——比如两节语文+一节班队活动+一节道法，一天摊开来算了4节，也不允许）
DAILY_PERIOD_CAP = {
    "语文": 3,
    "外语": 5, "音乐": 5, "体育与健康": 5, "美术": 5, "科学": 5,
    "信息科技": 5, "劳动&人工智能": 5, "生命.生态.安全&心理健康": 5,
    "习读本": 5, "道德与法治": 5, "综合实践": 5, "劳动": 5, "阅读": 5, "国际理解": 5,
}
MATH_ATTRIBUTED_SUBJECTS = {MATH, "综合实践", "劳动"}


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


def _add_daily_coverage_hard(model, key, idxs, day_vars, max_per_day=2):
    """要求 idxs 里的atom的day必须覆盖全部5天（每天至少1个）——真正的硬约束，不设退路。
    只在已确认没有物理冲突（比如共享老师+教研时间导致某天排不出）之后才应该用这个，
    否则应该用 _add_daily_coverage_soft。
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
        model.Add(sum(day_bools) >= 1)
        model.Add(sum(day_bools) <= max_per_day)


def _add_daily_total_cap(model, key, idxs, day_vars, cap):
    """要求 idxs 里的atom每天的总节数不超过cap（不要求连续，纯粹是当天的总数）。"""
    if not idxs:
        return
    for d in range(1, 6):
        day_bools = []
        for i in idxs:
            b = model.NewBoolVar(f"daycap_{key}_{i}_d{d}")
            model.Add(day_vars[(key, i)] == d).OnlyEnforceIf(b)
            model.Add(day_vars[(key, i)] != d).OnlyEnforceIf(b.Not())
            day_bools.append(b)
        model.Add(sum(day_bools) <= cap)


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
        # 点名老师的"除某天外必须上午"要求（当前连堂候选本来就都在上午，这里是防御性处理）
        for t in atom["teachers"]:
            exc_day = TEACHER_AM_ONLY_EXCEPT_DAY.get(t)
            if exc_day is not None:
                candidates = {s for s in candidates if s % 10 in AM_PERIODS or s // 10 == exc_day}
        return candidates

    # 常规裁剪：规则1（语文老师周二上午不排）、规则2（数学老师周三上午不排）、规则5（15人周一上午2-4节不排）
    domain = set(base)
    teachers = atom["teachers"]
    if any(t in class_teachers_are_chinese for t in teachers):
        domain -= TUE_AM_SLOTS
    if any(t in class_teachers_are_math for t in teachers):
        if grade_key in GRADE26 and atom["subject"] in (MATH, "综合实践"):
            # 用户明确放开的口子：一二年级数学课（含综合实践）允许部分班排在周三上午第4节（缓解同一数学
            # 老师带2个班时周三下午只有1个槎位、两个班抢不过来的问题），其余人/其余科目仍然周三上午全禁
            domain -= (WED_AM_SLOTS - {34})
        else:
            domain -= WED_AM_SLOTS
    if any(t in FORBIDDEN_MON_AM234 for t in teachers):
        domain -= MON_AM234_SLOTS

    # 语文、数学：除了各自的教研当天（语文=周二，数学=周三），其余天全部只能排上午（用户明确要求，硬约束）。
    # 数学老师教的课不止"数学"这一个科目名——综合实践、（部分年级的）劳动也是同一个数学老师教的，
    # 同样要受这条"非教研日必须上午"的约束，否则这部分课会被解出到下午，跟老师本人的排课意图不符。
    if atom["subject"] == CHINESE:
        am_only = {s for s in domain if s % 10 in AM_PERIODS}
        exception_pm = domain & _pm_slots_for_day(grade_key, 2)
        domain = am_only | exception_pm
    elif atom["subject"] in MATH_ATTRIBUTED_SUBJECTS and any(t in class_teachers_are_math for t in teachers):
        am_only = {s for s in domain if s % 10 in AM_PERIODS}
        exception_pm = domain & _pm_slots_for_day(grade_key, 3)
        domain = am_only | exception_pm

    # 点名老师的"除某天外必须上午"要求（用户：卢诗雨除周二外都排上午）——
    # 覆盖这位老师教的全部科目（不只是语文），combo槎位同样受约束。
    # 注意 banduihuodong_fixed（周一下午第1节）是全校固定槎，不受这条影响（在上面已提前return）。
    for t in teachers:
        exc_day = TEACHER_AM_ONLY_EXCEPT_DAY.get(t)
        if exc_day is not None:
            am_only = {s for s in domain if s % 10 in AM_PERIODS}
            exception_pm = domain & _pm_slots_for_day(grade_key, exc_day)
            domain = am_only | exception_pm

    return domain


def classify_teacher_for_caps(teacher, teacher_primaries, teacher_role_periods):
    """决定这位老师适用哪一档"连堂上限/每天课时上限"。
    规则（用户确认）：优先语文，其次数学，再其次按课时数最多的专职学科。
    """
    primaries = teacher_primaries.get(teacher, set())
    if "语文" in primaries:
        return "语文"
    if "数学" in primaries:
        return "数学"
    roles = teacher_role_periods.get(teacher, {})
    if not roles:
        return None
    return max(roles.items(), key=lambda kv: kv[1])[0]


def _add_teacher_run_and_daily_caps(model, teacher_pool, day_vars, per_vars,
                                     teacher_primaries, teacher_role_periods, max_period_by_grade):
    """需求1：限制同一位老师"连续节次"的长度，以及每天的总课时数。
    连堂按老师本人算，不看科目名——语文老师连着上"语文语文道法"就算3节连堂。
    做法：为每位老师、每天、每节次建一个"这节有课"的bool，然后对每个长度为 cap+1 的
    连续窗口要求"窗口内有课的节数 <= cap"，即不允许出现 cap+1 连堂。
    """
    stats = {"run_windows": 0, "daily_caps": 0, "teachers": 0}
    uncovered_categories = set()
    for teacher, pool in teacher_pool.items():
        category = classify_teacher_for_caps(teacher, teacher_primaries, teacher_role_periods)
        run_cap = RUN_CAP.get(category)
        daily_cap = DAILY_PERIOD_CAP.get(category)
        if run_cap is None and daily_cap is None:
            # 防御：不允许"某个学科分类忘了配上限"导致这批老师完全不受约束（之前习读本/道法就踩过这个坑）
            uncovered_categories.add(category)
            continue
        stats["teachers"] += 1

        max_period = max(max_period_by_grade[key[0]] for key, _ in pool)

        for d in range(1, 6):
            busy = {}
            for p in range(1, max_period + 1):
                flags = []
                for (key, idx) in pool:
                    b = model.NewBoolVar(f"busy_{teacher}_{key}_{idx}_d{d}p{p}")
                    is_day = model.NewBoolVar(f"bd_{teacher}_{key}_{idx}_d{d}p{p}")
                    model.Add(day_vars[(key, idx)] == d).OnlyEnforceIf(is_day)
                    model.Add(day_vars[(key, idx)] != d).OnlyEnforceIf(is_day.Not())
                    is_per = model.NewBoolVar(f"bp_{teacher}_{key}_{idx}_d{d}p{p}")
                    model.Add(per_vars[(key, idx)] == p).OnlyEnforceIf(is_per)
                    model.Add(per_vars[(key, idx)] != p).OnlyEnforceIf(is_per.Not())
                    model.AddBoolAnd([is_day, is_per]).OnlyEnforceIf(b)
                    model.AddBoolOr([is_day.Not(), is_per.Not()]).OnlyEnforceIf(b.Not())
                    flags.append(b)
                slot_busy = model.NewBoolVar(f"slotbusy_{teacher}_d{d}p{p}")
                model.AddMaxEquality(slot_busy, flags)
                busy[p] = slot_busy

            if daily_cap is not None:
                model.Add(sum(busy.values()) <= daily_cap)
                stats["daily_caps"] += 1

            if run_cap is not None:
                for start in range(1, max_period - run_cap + 1):
                    window = [busy[p] for p in range(start, start + run_cap + 1)]
                    model.Add(sum(window) <= run_cap)
                    stats["run_windows"] += 1
    if uncovered_categories:
        raise ValueError(
            f"以下学科分类没有配置连堂/每日课时上限，这批老师会完全不受约束，请补进 RUN_CAP/DAILY_PERIOD_CAP: "
            f"{sorted(c for c in uncovered_categories if c)}")
    return stats


def build_model(classes, all_atoms, grades_filter=None, enabled_rules=None):
    """构建CP-SAT模型。grades_filter为None时用全部81班，否则只用指定年级（用于分阶段验证）。
    enabled_rules: None表示全部规则开启；否则是一个set，只有在集合里的规则才生效（用于二分排查）。
    可选值: 'chinese_daily','math_daily','pe_bijection','no_double_subject','teacher_conflict',
           'function_room','cross_grade_adjacency'
    """
    if enabled_rules is None:
        enabled_rules = {"chinese_daily", "math_daily", "pe_bijection", "no_double_subject",
                          "teacher_conflict", "function_room", "cross_grade_adjacency",
                          "teacher_run_caps"}
    model = cp_model.CpModel()

    if grades_filter is not None:
        classes = [c for c in classes if c["grade_key"] in grades_filter]

    class_keys = build_class_key_list(classes)
    class_by_key = {(c["grade_key"], c["class_no"]): c for c in classes}

    # 教师画像（用于需求1的分档）：每位老师涉及的primary_subject集合，以及各学科的总课时数
    teacher_primaries = defaultdict(set)
    teacher_role_periods = defaultdict(lambda: defaultdict(float))
    for c in classes:
        for e in c["subjects"]:
            teacher_primaries[e["teacher_raw"]].add(e["col_primary_subject"])
            teacher_role_periods[e["teacher_raw"]][e["col_primary_subject"]] += e["periods"]

    max_period_by_grade = {gk: (6 if gk in GRADE26 else 7) for gk in CAMPUS_OF_GRADE}

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

        # 需求2：这个班里"数学老师教的课"（数学+综合实践+劳动）全周下午的总节数——
        # 硬上限2节，且超过1节就重罚，让求解器尽量压到1节（周三下午那节通常是唯一必须的）
        math_teachers_here = math_teachers_by_class[key]
        math_attributed_idxs = [
            i for i, a in enumerate(atoms)
            if a["subject"] in MATH_ATTRIBUTED_SUBJECTS and set(a["teachers"]) & math_teachers_here
        ]
        if math_attributed_idxs:
            pm_flags = []
            for i in math_attributed_idxs:
                is_pm = model.NewBoolVar(f"mathpm_{key}_{i}")
                model.Add(per_vars[(key, i)] >= 5).OnlyEnforceIf(is_pm)
                model.Add(per_vars[(key, i)] <= 4).OnlyEnforceIf(is_pm.Not())
                pm_flags.append(is_pm)
            model.Add(sum(pm_flags) <= 2)  # 硬上限：一个班最多2节数学系课在下午
            math_pm_excess = model.NewIntVar(0, 2, f"mathpmexcess_{key}")
            model.Add(math_pm_excess >= sum(pm_flags) - 1)
            penalty_terms.append((WEIGHT_MATH_PM_OVERFLOW, math_pm_excess, key, "math_pm_over_1"))

        # 需求(追加)：语文老师(含道法/班队活动/劳动等他/她教的其它科目)一天总课时不能超过3节——
        # 用户发现"语文语文+班会+道法"这种非连续但当天总数=4节的情况，也要卡住
        chinese_teachers_here = chinese_teachers_by_class[key]
        chinese_all_idxs = [
            i for i, a in enumerate(atoms) if set(a["teachers"]) & chinese_teachers_here
        ]
        _add_daily_total_cap(model, key, chinese_all_idxs, day_vars, cap=3)

        # 每天都要有语文课（规则7）：语文类atom(含连堂两节)的day尽量覆盖5天（软约束，见_add_daily_coverage_soft注释）
        if "chinese_daily" in enabled_rules:
            chinese_idxs = [i for i, a in enumerate(atoms) if a["subject"] == CHINESE]
            _add_daily_coverage_soft(model, key, chinese_idxs, day_vars, penalty_terms, WEIGHT_DAILY_CHINESE)

        # 每天都要有数学老师上的课（规则7，综合实践算数学老师的课，含combo2里那半是"综合实践"且由数学老师教的情况——
        # 六年级的综合实践0.5被合并进了劳动/劳动人工智能combo，那个slot单双周里有一半也是数学老师在教，同样算数学课）。
        # 用户明确要求这是"最起码"必须满足的——但实测发现某些年级（如二年级7对共享数学老师的班级组合在一起时）
        # 即便每一对单独可行，14个班放在一起硬性要求"每班每天都有"仍会数学上不可行（多班资源整体挤兑），
        # 所以保留软约束+超高权重的做法（几乎当硬约束用，但不会把整个模型拖到INFEASIBLE），
        # 已经用周三上午第4节的例外解决了共享数学老师的大部分物理冲突。
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

    # 需求1：同一老师的连堂上限 + 专职老师每天课时上限（硬约束）
    if "teacher_run_caps" in enabled_rules:
        cap_stats = _add_teacher_run_and_daily_caps(
            model, teacher_pool, day_vars, per_vars,
            teacher_primaries, teacher_role_periods, max_period_by_grade)
        print(f"  连堂/每日上限约束：覆盖{cap_stats['teachers']}位老师，"
              f"{cap_stats['run_windows']}个连堂窗口，{cap_stats['daily_caps']}条每日上限")

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
