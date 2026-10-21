"""只根据点评营业时间做可解释的时段校验；不推断实时营业状态。"""

from __future__ import annotations

import re


def requested_time(message: str) -> str | None:
    match = re.search(r"(?:(今晚|晚上|下午|中午|凌晨|早上|上午)\s*)?(\d{1,2})\s*(?:[:：](\d{2})|点(?:([0-5]?\d)分?)?|时(?:([0-5]?\d)分?)?)", message)
    if not match:
        return None
    period, hour_text, colon_minute, point_minute, hour_minute = match.groups()
    hour = int(hour_text)
    minute = int(colon_minute or point_minute or hour_minute or 0)
    if hour > 23 or minute > 59:
        return None
    if period in ("今晚", "晚上", "下午") and hour < 12:
        hour += 12
    elif period == "中午" and hour < 11:
        hour += 12
    elif period == "凌晨" and hour == 12:
        hour = 0
    elif period in ("早上", "上午") and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def minute_of_day(clock: str) -> int:
    hour, minute = map(int, clock.split(":"))
    return hour * 60 + minute


def display_time(total_minutes: int) -> str:
    label = f"{(total_minutes // 60) % 24:02d}:{total_minutes % 60:02d}"
    return label + ("（次日）" if total_minutes >= 1440 else "")


def open_for_visit(open_hours: str | None, start_minutes: int, duration_minutes: int) -> bool | None:
    """None 表示营业时间为空或无法解析；跨午夜时段支持从前一天延续。"""
    if not open_hours:
        return None
    intervals = []
    for segment in str(open_hours).split(","):
        # 支持「次日07:00」「次日 02:30」这类跨夜写法，营业时间到第二天。
        match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(次日)?\s*(\d{1,2}):(\d{2})\s*", segment)
        if not match:
            return None
        start_hour, start_minute, next_day, end_hour, end_minute = match.groups()
        start_hour, start_minute = int(start_hour), int(start_minute)
        end_hour, end_minute = int(end_hour), int(end_minute)
        if start_hour > 23 or end_hour > 24 or start_minute > 59 or end_minute > 59:
            return None
        if end_hour == 24 and end_minute != 0:
            return None
        left = start_hour * 60 + start_minute
        right = end_hour * 60 + end_minute + (1440 if next_day else 0)
        if right <= left:
            right += 1440
        intervals.append((left, right))
    for shift in (-1440, 0, 1440):
        if any(left + shift <= start_minutes and start_minutes + duration_minutes <= right + shift
               for left, right in intervals):
            return True
    return False


def default_duration(category: str) -> int:
    return 120 if category.lower() in ("ktv", "酒吧", "轰趴馆") else 90
