"""User-facing labels shared by the workbench and readable exports."""

from datetime import date

FIELD_LABELS = {
    "province": "所在省份",
    "province_confirmation": "省份确认",
    "province_confirmed": "省份已核对",
    "crop": "种植作物",
    "crop_confirmation": "作物确认",
    "crop_confirmed": "作物已核对",
    "stage": "生育期",
    "stage_confirmation": "现场生育期确认",
    "stage_confirmed": "生育期已现场核对",
    "field_type": "田块类型",
    "inspection_requested": "巡田安排",
    "maintenance_need_confirmed": "沟渠维护",
    "maintenance_problem_confirmed": "沟渠问题",
    "maintenance_purpose": "沟渠维护目的",
    "duration_minutes": "预计耗时",
    "conditions_confirmation": "必要条件确认",
    "resource_minutes": "可用工时",
    "site_workability": "现场能否作业",
    "weather_workability": "天气是否适合作业",
    "ditch_connectivity": "沟渠是否通畅",
    "standing_water": "田间积水情况",
    "pest_signs": "虫害迹象",
    "missing_seedlings": "缺苗情况",
    "crowding": "苗株拥挤情况",
    "field_profile": "地块基本信息",
    "time_intervals": "具体可用时段",
    "intervals": "具体可用时段",
    "inspection_duration_minutes": "巡田预计耗时",
    "maintenance_duration_minutes": "维护预计耗时",
    "actual_rain_observed": "实际降雨观察",
    "winter_before_context_confirmed": "冬前情境确认",
    "leaf_count": "叶片数",
    "moisture": "土壤墒情",
    "name": "地块名称",
    "area_mu": "面积",
    "inspection_priority": "巡田优先级",
    "maintenance_priority": "维护优先级",
    "inspection_earliest_date": "最早巡田日期",
    "maintenance_earliest_date": "最早维护日期",
    "inspection_due_date": "巡田目标日期",
    "maintenance_due_date": "维护目标日期",
}
CHANGE_LABELS = {
    "added": "新增",
    "moved": "移动",
    "removed": "移出",
    "cancelled": "取消",
    "unchanged": "保持不变",
    "completed": "已完成",
    "content_changed": "内容变化",
}


def missing_label(value: str) -> str:
    parts = value.rsplit(":", 1)
    label = FIELD_LABELS.get(parts[-1], "其他待核对信息")
    if len(parts) == 2:
        try:
            day = date.fromisoformat(parts[0])
        except ValueError:
            return label
        return f"{day:%m月%d日} · {label}"
    return label


def task_title(task_type: str) -> str:
    return {
        "inspection": "巡田检查",
        "maintenance": "沟渠维护",
        "custom": "自定义农事",
        "follow_up": "问题复查",
        "management": "农务管理",
    }.get(task_type, "农事任务")


def field_value_label(field: str, value: object) -> str:
    """Show saved values in the vocabulary of the relevant question."""
    if value is None:
        return (
            "未填写"
            if field in {"name", "province", "crop", "stage", "field_type", "area_mu"}
            else "暂不确定"
        )
    if isinstance(value, bool):
        if field in {"maintenance_need_confirmed", "inspection_requested"}:
            return "需要" if value else "不需要"
        if field == "maintenance_problem_confirmed":
            return "有" if value else "没有"
        return "是" if value else "否"
    choices = {
        "ditch_connectivity": {"clear": "通畅", "impaired": "受阻"},
        "moisture": {"dry": "偏干", "normal": "适中", "wet": "偏湿"},
        "maintenance_purpose": {
            "clear_existing_ditch": "清理沟渠，改善排水",
            "other": "其他目的",
        },
        "field_type": {
            "lowland_rice_stubble": "低洼/稻茬田",
            "dry_hilly": "丘陵旱地",
            "other": "其他",
        },
    }
    if field in choices:
        return choices[field].get(str(value), str(value))
    if field == "area_mu":
        return f"{value} 亩"
    if field.endswith("duration_minutes"):
        return f"{value} 分钟"
    return str(value)


def proposal_change_rows(changes: dict[str, object]) -> list[str]:
    """List every proposed change, including future fields without a known label."""
    rows: list[str] = []
    for field, change in changes.items():
        label = FIELD_LABELS.get(field, f"其他信息（{field}）")
        if isinstance(change, dict) and "old" in change and "new" in change:
            old = field_value_label(field, change["old"])
            new = field_value_label(field, change["new"])
            rows.append(f"{label}：{old} → {new}")
        else:
            rows.append(f"{label}：{field_value_label(field, change)}")
    return rows
