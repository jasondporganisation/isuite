from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

CHASE_DAYS = (3, 7, 14)
CHASE_WINDOW_DAYS = CHASE_DAYS[-1]  # a lead is chased for this many days after capture
SGT = ZoneInfo("Asia/Singapore")


def next_due(captured_on: date, chase_step: int) -> date | None:
    """chase_step = messages sent so far. 0 -> due on capture day; 1-3 -> +3/+7/+14; >=4 -> None."""
    if chase_step <= 0:
        return captured_on
    if chase_step > len(CHASE_DAYS):
        return None
    return captured_on + timedelta(days=CHASE_DAYS[chase_step - 1])


def sg_today(now_utc: datetime) -> date:
    return now_utc.astimezone(SGT).date()
