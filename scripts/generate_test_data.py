import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DB_FILE = ROOT / "coach_data.json"
BACKUP_FILE = ROOT / "coach_data.backup.json"


def new_id(rng: random.Random) -> str:
    # deterministic-ish for repeatability; still unique enough for local test data
    return f"{rng.getrandbits(128):032x}"


def dt_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def daterange(start: datetime, end: datetime):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


@dataclass(frozen=True)
class Slot:
    weekday: int  # 0=Mon
    start_hour: int

    def label(self) -> str:
        return f"{self.weekday}-{self.start_hour:02d}"


def build_empty_db(now: datetime, rng: random.Random) -> dict[str, Any]:
    coach_id = new_id(rng)
    return {
        "schema_version": 1,
        "students": [],
        "lessons": [],
        "lesson_types": [],
        "payments": [],
        "coaches": [
            {
                "id": coach_id,
                "name": "Coach",
                "contact": "",
                "created_at": dt_str(now),
                "venues": ["KF", "OCBC", "ActiveSG", "CCK"],
            }
        ],
        "finance_reports": [],
    }


def add_lesson_types(db: dict[str, Any]):
    # Prices are per person per lesson (2 hours), in SGD.
    per_lesson_price = {
        "1:1": 250.0,
        "1:2": 130.0,
        "1:3": 95.0,
        "1:4": 80.0,
    }

    # Store as per-person hourly rate to keep hours, price, participants separate.
    # fee = hours * price_per_hour_per_person * participants
    lesson_types = []
    for code, price in per_lesson_price.items():
        participants = int(code.split(":")[1])
        lesson_types.append(
            {
                "id": None,  # filled later
                "code": code,
                "label": code,
                "participants": participants,
                "price_per_hour": price / 2.0,
                "currency": "SGD",
                "active": True,
            }
        )

    db["lesson_types"] = lesson_types


def choose_daily_slots(rng: random.Random) -> list[int]:
    # start hours for 2-hour lessons
    all_slots = [9, 11, 14, 16, 18]
    k = rng.randint(3, 5)
    return sorted(rng.sample(all_slots, k=k))


def main():
    rng = random.Random(20260430)
    now = datetime.now().replace(second=0, microsecond=0)

    if DB_FILE.exists():
        BACKUP_FILE.write_text(DB_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Backed up to {BACKUP_FILE}")

    db = build_empty_db(now, rng)
    add_lesson_types(db)

    # Fill ids
    for lt in db["lesson_types"]:
        lt["id"] = new_id(rng)

    coach_id = db["coaches"][0]["id"]
    lt_by_code = {lt["code"]: lt for lt in db["lesson_types"]}
    venues = list(db["coaches"][0].get("venues") or [])

    # Students
    first_names = [
        "Amy", "Alex", "David", "Ben", "Chris", "Ethan", "Iris", "Judy", "Kevin", "Lily",
        "Mia", "Noah", "Olivia", "Peter", "Quinn", "Ryan", "Sophie", "Tom", "Uma", "Victor",
        "Wendy", "Yuki", "Zoe", "小王", "小李", "小张", "阿明", "阿杰",
    ]
    rng.shuffle(first_names)
    student_names = first_names[:20]

    students = []
    student_id_by_name = {}
    for name in student_names:
        sid = new_id(rng)
        student_id_by_name[name] = sid
        preferred_venue = rng.choice(venues) if venues else ""
        students.append(
            {
                "id": sid,
                "name": name,
                "contact": "",
                "note": "",
                "venue": preferred_venue,
                "created_at": dt_str(datetime(2026, 1, 1, 9, 0)),
                "archived": False,
            }
        )
    db["students"] = students

    # Generate lessons from Jan 1 to Apr 30, 2026, with weekly cap (3-4 lessons/week total).
    weekdays = [0, 1, 2, 3, 4]  # teach Mon-Fri
    start_hours = [9, 11, 14, 16, 18]
    start_date = datetime(2026, 1, 1)
    end_date = datetime(2026, 4, 30, 23, 59)
    lessons = []
    payments: list[dict[str, Any]] = []

    def week_start(d: datetime) -> datetime:
        return (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

    # rotate students so everyone gets lessons over time
    roster = student_names[:]
    rng.shuffle(roster)
    roster_i = 0

    def next_students(k: int) -> list[str]:
        nonlocal roster_i
        picked = []
        for _ in range(k):
            picked.append(roster[roster_i % len(roster)])
            roster_i += 1
        return picked

    cur = week_start(start_date)
    while cur <= end_date:
        # pick 3-4 lesson slots in this week
        lesson_count = rng.randint(3, 4)
        possible_days = [cur + timedelta(days=i) for i in range(7) if (cur + timedelta(days=i)).weekday() in weekdays]
        rng.shuffle(possible_days)
        chosen_days = possible_days[:lesson_count]

        for day in chosen_days:
            h = rng.choice(start_hours)
            start_at = day.replace(hour=h, minute=0, second=0, microsecond=0)
            if start_at > end_date:
                continue

            # actual participants 1-4 (can vary; pricing uses actual count/type)
            n = rng.choices([1, 2, 3, 4], weights=[0.45, 0.30, 0.18, 0.07], k=1)[0]
            class_code = f"1:{n}"

            # make distribution fairer across students while still random-ish
            participants = next_students(n)
            # small chance to swap one participant to add variety
            if rng.random() < 0.25 and len(student_names) > n:
                participants[-1] = rng.choice([x for x in student_names if x not in participants])
            participant_ids = [student_id_by_name[p] for p in participants]

            topics_bank = ["发球", "步伐", "反手", "正手", "高远球", "接杀", "网前", "拉吊", "多球", "体能", "启动"]
            topics = rng.sample(topics_bank, k=rng.randint(2, 4))
            status = rng.choice(["状态不错", "有进步", "有点累", "未说明"])
            next_plan = rng.choice(["加强步伐", "提高反手稳定性", "网前搓放", "启动更快", "提升高远球质量", "未说明"])
            venue = rng.choice(venues) if venues else ""

            lesson = {
                "id": new_id(rng),
                "coach_id": coach_id,
                "lesson_type_id": lt_by_code[class_code]["id"],
                "start_at": dt_str(start_at),
                "duration_minutes": 120,
                "participant_ids": participant_ids,
                "participant_names_raw": [],
                "topics": topics,
                "status": status,
                "next_plan_time": rng.choice([None, "下周", "下次"]),
                "next_plan": next_plan,
                "original_text": "",
                "venue": venue,
                "created_at": dt_str(start_at),
                "updated_at": dt_str(now),
            }
            lessons.append(lesson)

            # Payments: per lesson per participant. Some paid, some unpaid.
            paid_prob = 0.72
            for sid in participant_ids:
                if rng.random() < paid_prob:
                    payments.append(
                        {
                            "id": new_id(rng),
                            "student_id": sid,
                            "lesson_id": lesson["id"],
                            "amount": None,
                            "currency": "SGD",
                            "paid_at": dt_str(start_at + timedelta(hours=2)),
                            "note": "",
                            "created_at": dt_str(start_at + timedelta(hours=2)),
                        }
                    )

        cur += timedelta(days=7)

    db["lessons"] = lessons
    db["payments"] = payments

    DB_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {len(db['students'])} students, {len(db['lessons'])} lessons, {len(db['payments'])} payments to {DB_FILE}"
    )


if __name__ == "__main__":
    main()

