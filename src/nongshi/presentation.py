"""User-facing labels shared by the workbench and readable exports."""

from datetime import date

FIELD_LABELS = {
    "province": "所在省份",
    "province_confirmation": "省份确认",
    "crop": "种植作物",
    "crop_confirmation": "作物确认",
    "stage": "生育期",
    "stage_confirmation": "现场生育期确认",
    "field_type": "田块类型",
    "inspection_requested": "是否需要巡田",
    "maintenance_need_confirmed": "是否需要沟渠维护",
    "maintenance_problem_confirmed": "是否存在需处理的沟渠问题",
    "maintenance_purpose": "沟渠维护目的",
    "duration_minutes": "预计耗时",
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
}
CHANGE_LABELS = {
    "added": "新增",
    "moved": "移动",
    "removed": "移出",
    "cancelled": "取消",
    "unchanged": "保持不变",
    "completed": "已完成",
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
    return {"inspection": "巡田与沟渠检查", "maintenance": "沟渠维护"}.get(task_type, "农事任务")
