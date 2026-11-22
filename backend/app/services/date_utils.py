"""日期范围工具：回填缺口按天展开，统一在这里处理。"""
from __future__ import annotations

import datetime as _dt


def parse_date(value: str) -> _dt.date:
    """把 YYYY-MM-DD 字符串解析成 date；非法值抛出 ValueError。"""
    return _dt.datetime.strptime(value.strip(), "%Y-%m-%d").date()


def daterange(start: str, end: str) -> list[str]:
    """返回闭区间 [start, end] 内的全部日期，格式 YYYY-MM-DD。"""
    start_day = parse_date(start)
    end_day = parse_date(end)
    if end_day < start_day:
        raise ValueError(f"日期范围非法：起始 {start} 晚于结束 {end}")
    days = (end_day - start_day).days
    return [
        (start_day + _dt.timedelta(days=offset)).isoformat()
        for offset in range(days + 1)
    ]


def merge_ranges(dates: list[str]) -> list[dict[str, str]]:
    """把离散日期合并成连续日期区间，便于展示可继续执行的范围。"""
    if not dates:
        return []
    ordered = sorted(dates)
    ranges: list[dict[str, str]] = []
    range_start = ordered[0]
    prev = parse_date(ordered[0])
    for day_text in ordered[1:]:
        current = parse_date(day_text)
        if (current - prev).days == 1:
            prev = current
            continue
        ranges.append({"start": range_start, "end": prev.isoformat()})
        range_start = day_text
        prev = current
    ranges.append({"start": range_start, "end": prev.isoformat()})
    return ranges
