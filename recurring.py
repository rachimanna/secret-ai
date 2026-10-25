"""Explicit recurring reminder schedules in the user's local timezone."""
import json
import re
from datetime import datetime, timedelta
import daily

DAYS = {"понедельник":0,"вторник":1,"среду":2,"среда":2,"четверг":3,"пятницу":4,"пятница":4,
        "субботу":5,"суббота":5,"воскресенье":6,"понедельникам":0,"вторникам":1,"средам":2,"четвергам":3,"пятницам":4,"субботам":5,"воскресеньям":6,
        "monday":0,"tuesday":1,"wednesday":2,"thursday":3,"friday":4,"saturday":5,"sunday":6,"mondays":0,"tuesdays":1,"wednesdays":2,"thursdays":3,"fridays":4,"saturdays":5,"sundays":6}


def parse(value, tz):
    value = " ".join(value.lower().strip().split())
    match = re.fullmatch(r"(?:каждый день|ежедневно|daily|every day|по будням|weekdays) (?:в )?(\d{2}:\d{2})", value)
    if match:
        return {"time":daily.clock(match[1]),"tz":tz,
                "days":[0,1,2,3,4] if value.startswith(("по будням","weekdays")) else list(range(7))}
    match = re.fullmatch(r"(?:каждый|каждую|по|every) (\w+) (?:в )?(\d{2}:\d{2})", value)
    if match and match[1] in DAYS:
        return {"time":daily.clock(match[2]),"tz":tz,"days":[DAYS[match[1]]]}
    return None


def next_at(rule, after):
    if isinstance(rule,str):
        rule=json.loads(rule)
    tz=daily.zone(rule["tz"])
    local=datetime.fromtimestamp(after,tz)
    hour,minute=map(int,rule["time"].split(":"))
    for offset in range(9):
        date=local.date()+timedelta(days=offset)
        if date.weekday() not in rule["days"]:
            continue
        candidate=datetime(date.year,date.month,date.day,hour,minute,tzinfo=tz)
        stamp=candidate.timestamp()
        # Round-trip resolves nonexistent local times at the spring DST transition.
        if stamp>after:
            return int(stamp)
    raise daily.DailyError("time")
