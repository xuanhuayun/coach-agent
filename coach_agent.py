from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from flask import Flask, abort, redirect, render_template, request, session, url_for

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]

DB_FILE = Path("coach_data.json")

SCHEMA_VERSION = 1
BASE_LESSON_MINUTES = 120  # legacy baseline; keep for migrating older data


def _baseline_minutes_for_class_type(code: str) -> int:
    """
    Pricing baseline per class type:
    - 1:1 is a 1-hour baseline
    - 1:2 / 1:3 / 1:4 are 2-hour baseline
    """
    return 60 if str(code or "").strip() == "1:1" else 120


def _normalize_class_type_code(code: str) -> str:
    """
    Normalize class type code to one of: 1:1 / 1:2 / 1:3 / 1:4
    Accepts common variants like full-width colon '：'.
    """
    s = str(code or "").strip()
    if not s:
        return ""
    s = s.replace("：", ":").replace(" ", "")
    if s in ("1:1", "1:2", "1:3", "1:4"):
        return s
    return ""


def _skip_new_student_map() -> dict[str, int]:
    v = session.get("skip_new_student_1h")
    if isinstance(v, dict):
        out: dict[str, int] = {}
        for k, ts in v.items():
            try:
                out[str(k)] = int(ts)
            except Exception:
                continue
        return out
    return {}


def _skip_new_student_set(names: list[str], seconds: int = 3600) -> None:
    m = _skip_new_student_map()
    now_ts = int(datetime.now().timestamp())
    exp = now_ts + int(seconds)
    for n in names:
        n = str(n or "").strip()
        if n:
            m[n] = exp
    session["skip_new_student_1h"] = m


def _skip_new_student_allowed(name: str) -> bool:
    n = str(name or "").strip()
    if not n:
        return False
    m = _skip_new_student_map()
    exp = m.get(n)
    if not exp:
        return False
    return int(datetime.now().timestamp()) <= int(exp)


def _is_draft_lesson(l: dict[str, Any]) -> bool:
    return bool(l.get("draft"))


def _lessons(db: dict[str, Any], include_drafts: bool = False) -> list[dict[str, Any]]:
    lessons = [l for l in db.get("lessons", []) if isinstance(l, dict)]
    if include_drafts:
        return lessons
    return [l for l in lessons if not _is_draft_lesson(l)]


SUPPORTED_LANGS = ("en", "zh")

TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        "app_name": "Coach Assistant",
        "nav_add_student": "Add Student",
        "nav_record_lesson": "Record Lesson",
        "nav_payments": "Payments",
        "nav_search": "Search",
        "nav_stats": "Stats",
        "nav_settings": "Settings",
        "lang_en": "EN",
        "lang_zh": "中文",
        "error_student_not_found": "Student not found",
        "error_name_required": "Student name is required",
        "error_lesson_text_required": "Lesson text is required",
    },
    "zh": {
        "app_name": "教练助手",
        "nav_add_student": "新增学员",
        "nav_record_lesson": "记录课程",
        "nav_payments": "付款状态",
        "nav_search": "学员搜索",
        "nav_stats": "财务统计",
        "nav_settings": "设置",
        "lang_en": "EN",
        "lang_zh": "中文",
        "error_student_not_found": "没有找到这个学员",
        "error_name_required": "学员姓名不能为空",
        "error_lesson_text_required": "课程记录不能为空",
    },
}


def _now_minute() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")

DEFAULT_CLASS_TYPES: dict[str, float] = {"1:1": 0.0, "1:2": 0.0, "1:3": 0.0, "1:4": 0.0}


def _parse_dt(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None


def _duration_to_hours(duration: str | None) -> float:
    if not duration or not isinstance(duration, str):
        return 0.0
    d = duration.strip().lower()

    m = re.search(r"(\d{1,3})\s*分钟", d)
    if m:
        return float(m.group(1)) / 60.0

    if "半小时" in d:
        return 0.5

    m = re.search(r"(\d+(?:\.\d+)?)\s*小时", d)
    if m:
        return float(m.group(1))

    m = re.search(r"(\d+(?:\.\d+)?)\s*h\b", d)
    if m:
        return float(m.group(1))

    # fallback: pure number like "2"
    if re.fullmatch(r"\d+(?:\.\d+)?", d):
        return float(d)

    return 0.0


def _month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def _range_to_start_end(range_key: str, start: str | None, end: str | None) -> tuple[datetime | None, datetime | None]:
    now = datetime.now()
    if range_key == "month":
        return datetime(now.year, now.month, 1), None
    if range_key == "ytd":
        return datetime(now.year, 1, 1), None
    if range_key in ("3m", "6m", "12m"):
        months = int(range_key[:-1])
        # approximate month as 30 days for MVP
        return now.replace(microsecond=0) - timedelta(days=30 * months), None
    if range_key == "custom":
        return _parse_dt(start), _parse_dt(end)
    if range_key == "all":
        return None, None
    # default 3 months
    return now.replace(microsecond=0) - timedelta(days=90), None


def _lesson_in_range(lesson: dict[str, Any], start_dt: datetime | None, end_dt: datetime | None) -> bool:
    dt = _parse_dt(lesson.get("date"))
    if not dt:
        return True  # keep weird legacy rows rather than hiding data
    if start_dt and dt < start_dt:
        return False
    if end_dt and dt > end_dt:
        return False
    return True


def _new_id() -> str:
    return uuid.uuid4().hex


def _ensure_v1_shape(db: dict[str, Any]) -> dict[str, Any]:
    """
    Ensure database has the stable top-level tables.
    Tables:
      - students
      - lessons
      - lesson_types
      - payments
      - coaches
      - finance_reports
    """
    db.setdefault("schema_version", SCHEMA_VERSION)
    for k in ("students", "lessons", "lesson_types", "payments", "coaches", "finance_reports"):
        if k not in db or not isinstance(db.get(k), list):
            db[k] = []
    return db


def _dedupe_students_by_name(db: dict[str, Any]) -> bool:
    students = db.get("students", [])
    if not isinstance(students, list):
        return False
    seen: dict[str, str] = {}
    id_map: dict[str, str] = {}
    changed = False

    new_students: list[dict[str, Any]] = []
    for s in students:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "")
        name = str(s.get("name") or "").strip()
        if not sid or not name:
            continue
        if name in seen:
            id_map[sid] = seen[name]
            changed = True
            continue
        seen[name] = sid
        new_students.append(s)

    if changed:
        db["students"] = new_students
        # update lessons participant_ids
        lessons = db.get("lessons", [])
        if isinstance(lessons, list):
            for l in lessons:
                if not isinstance(l, dict):
                    continue
                pids = l.get("participant_ids")
                if not isinstance(pids, list):
                    continue
                new_pids = []
                for pid in pids:
                    pid = str(pid)
                    new_pids.append(id_map.get(pid, pid))
                l["participant_ids"] = new_pids
        # update payments
        payments = db.get("payments", [])
        if isinstance(payments, list):
            for p in payments:
                if isinstance(p, dict) and p.get("student_id"):
                    sid = str(p["student_id"])
                    p["student_id"] = id_map.get(sid, sid)

    return changed


def _clean_db(db: dict[str, Any]) -> bool:
    """
    Best-effort, idempotent cleanup to remove/repair obvious dirty data.
    Returns True if modifications were made.
    """
    changed = False
    _ensure_v1_shape(db)

    # Ensure lesson_types exist and have required fields
    if not db.get("lesson_types"):
        for code in DEFAULT_CLASS_TYPES.keys():
            participants = 1
            try:
                participants = int(code.split(":")[1])
            except Exception:
                participants = 1
            db["lesson_types"].append(
                {
                    "id": _new_id(),
                    "code": code,
                    "label": code,
                    "participants": participants,
                    "price_per_hour": 0.0,
                    "price_per_lesson": 0.0,
                    "currency": "SGD",
                    "active": True,
                }
            )
        changed = True
    else:
        # ensure each lesson_type has price_per_lesson (migrate from old hourly)
        for lt in db.get("lesson_types", []):
            if not isinstance(lt, dict):
                continue
            if "price_per_lesson" not in lt:
                try:
                    baseline = _baseline_minutes_for_class_type(str(lt.get("code") or ""))
                    lt["price_per_lesson"] = float(lt.get("price_per_hour") or 0.0) * (baseline / 60.0)
                except Exception:
                    lt["price_per_lesson"] = 0.0
                changed = True

    # Ensure a default coach exists
    if not db.get("coaches"):
        db["coaches"].append({"id": _new_id(), "name": "Default", "contact": "", "created_at": _now_minute()})
        changed = True
    else:
        # Ensure venues config exists on coach
        coach0 = db["coaches"][0] if isinstance(db.get("coaches"), list) and db["coaches"] else None
        if isinstance(coach0, dict) and "venues" not in coach0:
            coach0["venues"] = []
            changed = True
    venues_cfg = []
    try:
        coach0 = db["coaches"][0] if isinstance(db.get("coaches"), list) and db["coaches"] else None
        if isinstance(coach0, dict) and isinstance(coach0.get("venues"), list):
            venues_cfg = [str(v).strip() for v in coach0.get("venues") if str(v).strip()]
    except Exception:
        venues_cfg = []

    # Dedupe students
    if _dedupe_students_by_name(db):
        changed = True

    # Build student index
    students = db.get("students", [])
    by_name: dict[str, dict[str, Any]] = {}
    by_id: dict[str, dict[str, Any]] = {}
    if isinstance(students, list):
        for s in students:
            if isinstance(s, dict) and s.get("id") and s.get("name"):
                by_name[str(s["name"])] = s
                by_id[str(s["id"])] = s
                if "note" not in s:
                    s["note"] = ""
                    changed = True
                if "venue" not in s:
                    s["venue"] = ""
                    changed = True
                if "default_class_type" not in s:
                    s["default_class_type"] = "1:1"
                    changed = True
                else:
                    norm_ct = _normalize_class_type_code(str(s.get("default_class_type") or ""))
                    if norm_ct and norm_ct != str(s.get("default_class_type") or ""):
                        s["default_class_type"] = norm_ct
                        changed = True
                if venues_cfg and not str(s.get("venue") or "").strip():
                    s["venue"] = venues_cfg[0]
                    changed = True

    # Ensure lessons have required fields and resolve participant IDs
    lessons = db.get("lessons", [])
    lt_ids = {str(lt.get("id")) for lt in db.get("lesson_types", []) if isinstance(lt, dict) and lt.get("id")}
    default_lt_id = next(iter(lt_ids), None)
    default_coach_id = str((db.get("coaches") or [{}])[0].get("id") or "")

    if isinstance(lessons, list):
        for l in lessons:
            if not isinstance(l, dict):
                continue
            if "draft" not in l:
                l["draft"] = False
                changed = True
            if not l.get("id"):
                l["id"] = _new_id()
                changed = True
            if not l.get("coach_id"):
                l["coach_id"] = default_coach_id
                changed = True
            if not l.get("lesson_type_id") or str(l.get("lesson_type_id")) not in lt_ids:
                l["lesson_type_id"] = default_lt_id
                changed = True

            if "class_type" in l:
                norm_lct = _normalize_class_type_code(str(l.get("class_type") or ""))
                if norm_lct != str(l.get("class_type") or ""):
                    l["class_type"] = norm_lct
                    changed = True

            if not l.get("start_at"):
                l["start_at"] = l.get("created_at") or _now_minute()
                changed = True

            dm = l.get("duration_minutes")
            if dm is not None and not (isinstance(dm, int) and dm >= 0):
                l["duration_minutes"] = None
                changed = True

            if "topics" not in l or not isinstance(l.get("topics"), list):
                l["topics"] = []
                changed = True
            if "venue" not in l:
                l["venue"] = ""
                changed = True
            # Do not auto-fill venue for drafts or lessons without a resolved student.
            # Venue should stay empty until coach confirms it on the edit page.
            pids = l.get("participant_ids")
            has_student = isinstance(pids, list) and len(pids) > 0
            if (
                venues_cfg
                and not str(l.get("venue") or "").strip()
                and not bool(l.get("draft"))
                and has_student
            ):
                # prefer first participant's preferred venue, else default venue
                picked = None
                s0 = by_id.get(str(pids[0]))
                if isinstance(s0, dict) and str(s0.get("venue") or "").strip():
                    picked = str(s0.get("venue")).strip()
                l["venue"] = picked or venues_cfg[0]
                changed = True

            # Ensure per-lesson price snapshot exists (locks historical price)
            # prefer price_per_lesson snapshot; migrate from old price_per_hour snapshot if needed
            if l.get("price_per_lesson") is None and l.get("price_per_hour") is not None:
                try:
                    baseline = (
                        int(l.get("baseline_minutes"))
                        if isinstance(l.get("baseline_minutes"), int) and int(l.get("baseline_minutes")) > 0
                        else BASE_LESSON_MINUTES
                    )
                    l["price_per_lesson"] = float(l.get("price_per_hour") or 0.0) * (baseline / 60.0)
                    changed = True
                except Exception:
                    pass

            if l.get("price_per_lesson") is None or not str(l.get("currency") or "").strip():
                lt_id = str(l.get("lesson_type_id") or "")
                for lt in db.get("lesson_types", []):
                    if isinstance(lt, dict) and str(lt.get("id") or "") == lt_id:
                        try:
                            l["price_per_lesson"] = float(lt.get("price_per_lesson") or 0.0)
                        except Exception:
                            l["price_per_lesson"] = 0.0
                        l["currency"] = str(lt.get("currency") or "SGD")
                        # keep hourly snapshot for backward compatibility
                        try:
                            baseline = (
                                int(l.get("baseline_minutes"))
                                if isinstance(l.get("baseline_minutes"), int) and int(l.get("baseline_minutes")) > 0
                                else BASE_LESSON_MINUTES
                            )
                            l["price_per_hour"] = float(l.get("price_per_lesson") or 0.0) / (baseline / 60.0)
                        except Exception:
                            l["price_per_hour"] = l.get("price_per_hour") or 0.0
                        changed = True
                        break

            # Baseline minutes snapshot (keeps historical fee calculations stable)
            if "baseline_minutes" not in l:
                # Existing historical data used a 2-hour baseline across the board; preserve that.
                l["baseline_minutes"] = BASE_LESSON_MINUTES
                changed = True

            # Fix common next-plan parsing issue: date accidentally stored in next_plan
            npt = str(l.get("next_plan_time") or "").strip()
            np = str(l.get("next_plan") or "").strip()
            np_date = re.sub(r"^(是|在|于)\s*", "", np).strip()
            if np_date and re.fullmatch(r"\d{1,2}月\d{1,2}[号日]|\d{1,2}[/-]\d{1,2}|\d{4}-\d{1,2}-\d{1,2}", np_date) and (
                not npt or npt in ("下次", "下周", "下回", "未说明")
            ):
                # Reuse the splitter normalizer by constructing "下次" + date
                t, content, _ = _split_next_plan(f"下次{np_date}")
                if t:
                    l["next_plan_time"] = t
                    l["next_plan"] = "" if content in (None, "未说明") else str(content)
                    changed = True

            # resolve participant_ids using participant_names_raw (single name case)
            pids = l.get("participant_ids")
            raw = l.get("participant_names_raw")
            if (not pids or not isinstance(pids, list) or len(pids) == 0) and isinstance(raw, list) and raw:
                # only resolve first name for now
                name0 = str(raw[0] or "").strip()
                if name0:
                    s = by_name.get(name0)
                    if not s:
                        sid = _new_id()
                        s = {"id": sid, "name": name0, "contact": "", "created_at": _now_minute(), "archived": False}
                        db["students"].append(s)
                        by_name[name0] = s
                        by_id[sid] = s
                        changed = True
                    l["participant_ids"] = [str(s["id"])]
                    l["participant_names_raw"] = []
                    changed = True

    # Remove payments pointing to missing students
    payments = db.get("payments", [])
    if isinstance(payments, list):
        before = len(payments)
        db["payments"] = [p for p in payments if isinstance(p, dict) and str(p.get("student_id") or "") in by_id]
        if len(db["payments"]) != before:
            changed = True

    return changed


def _migrate_v0_to_v1(old: dict[str, Any]) -> dict[str, Any]:
    """
    Migrate legacy structure:
      - students: {name: {phone, paid, created_at}}
      - lessons: [{student, duration, training, ... class_type}]
      - settings.class_types: {"1:1": price, ...}
    into stable v1 tables.
    """
    v1: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    _ensure_v1_shape(v1)

    # Coaches: create one default coach
    default_coach_id = _new_id()
    v1["coaches"].append(
        {"id": default_coach_id, "name": "Default", "contact": "", "created_at": _now_minute()}
    )

    # Lesson types
    class_prices = {}
    settings = old.get("settings")
    if isinstance(settings, dict):
        ct = settings.get("class_types")
        if isinstance(ct, dict):
            class_prices = ct

    lesson_type_by_code: dict[str, str] = {}
    for code in DEFAULT_CLASS_TYPES.keys():
        price = class_prices.get(code, DEFAULT_CLASS_TYPES[code])
        try:
            price_f = float(price)
        except Exception:
            price_f = 0.0
        # participants inferred from "1:3" -> 3
        participants = 1
        try:
            participants = int(code.split(":")[1])
        except Exception:
            participants = 1
        lt_id = _new_id()
        v1["lesson_types"].append(
            {
                "id": lt_id,
                "code": code,
                "label": code,
                "participants": participants,
                "price_per_hour": price_f,
                "currency": "SGD",
                "active": True,
            }
        )
        lesson_type_by_code[code] = lt_id

    # Students
    student_by_name: dict[str, str] = {}
    legacy_students = old.get("students")
    if isinstance(legacy_students, dict):
        for name, info in legacy_students.items():
            sid = _new_id()
            student_by_name[str(name)] = sid
            phone = ""
            created_at = _now_minute()
            legacy_paid = False
            if isinstance(info, dict):
                phone = str(info.get("phone") or "")
                created_at = str(info.get("created_at") or created_at)
                legacy_paid = bool(info.get("paid"))
            v1["students"].append(
                {
                    "id": sid,
                    "name": str(name),
                    "contact": phone,
                    "created_at": created_at,
                    "archived": False,
                }
            )
            # Legacy paid flag becomes a zero-amount "status" payment record (keeps history without mixing fields)
            if legacy_paid:
                v1["payments"].append(
                    {
                        "id": _new_id(),
                        "student_id": sid,
                        "amount": 0.0,
                        "currency": "SGD",
                        "paid_at": created_at,
                        "method": "legacy",
                        "note": "Legacy paid flag (no amount provided).",
                        "lesson_ids": [],
                    }
                )

    # Lessons
    legacy_lessons = old.get("lessons")
    if isinstance(legacy_lessons, list):
        for l in legacy_lessons:
            if not isinstance(l, dict):
                continue
            lesson_id = str(l.get("id") or _new_id())
            student_name = str(l.get("student") or "未知")
            participant_ids: list[str] = []
            if student_name in student_by_name and student_name != "未知":
                participant_ids = [student_by_name[student_name]]
            # duration -> minutes
            hours = _duration_to_hours(str(l.get("duration") or ""))
            duration_minutes = int(round(hours * 60))
            training = str(l.get("training") or "")
            topics = [t.strip() for t in re.split(r"[、,，/]+", training) if t.strip()] if training else []
            code = str(l.get("class_type") or "1:1")
            lt_id = lesson_type_by_code.get(code) or lesson_type_by_code.get("1:1")

            v1["lessons"].append(
                {
                    "id": lesson_id,
                    "coach_id": default_coach_id,
                    "lesson_type_id": lt_id,
                    "start_at": str(l.get("date") or _now_minute()),
                    "duration_minutes": duration_minutes if duration_minutes > 0 else None,
                    "participant_ids": participant_ids,
                    "participant_names_raw": [] if participant_ids else ([student_name] if student_name else []),
                    "topics": topics,
                    "status": str(l.get("status") or "未说明") or "未说明",
                    "next_plan_time": str(l.get("next_plan_time") or "") or None,
                    "next_plan": str(l.get("next_plan") or "未说明") or "未说明",
                    "original_text": str(l.get("original_text") or ""),
                    "created_at": str(l.get("date") or _now_minute()),
                    "updated_at": _now_minute(),
                }
            )

    return v1


def _is_legacy_db(data: dict[str, Any]) -> bool:
    # v0 had students as dict; v1 students as list + schema_version
    if isinstance(data.get("students"), dict):
        return True
    if data.get("schema_version") != SCHEMA_VERSION:
        # Treat unknown as legacy for now
        return True
    return False


def _students_by_id(db: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["id"]: s for s in db.get("students", []) if isinstance(s, dict) and s.get("id")}


def _lesson_type_by_id(db: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {lt["id"]: lt for lt in db.get("lesson_types", []) if isinstance(lt, dict) and lt.get("id")}


def _lesson_type_id_by_code(db: dict[str, Any], code: str) -> str | None:
    for lt in db.get("lesson_types", []):
        if isinstance(lt, dict) and str(lt.get("code") or "") == code:
            return str(lt.get("id"))
    return None


def _lesson_view(db: dict[str, Any], lesson: dict[str, Any]) -> dict[str, Any]:
    """
    Compatibility view used by existing templates/routes.
    Produces keys like: id, date, student, duration, training, status, next_plan_time, next_plan
    """
    students = _students_by_id(db)
    lt_map = _lesson_type_by_id(db)
    l = dict(lesson)
    participant_ids = l.get("participant_ids") or []
    student_name = ""
    if participant_ids and isinstance(participant_ids, list):
        sid = participant_ids[0]
        student_name = str(students.get(sid, {}).get("name") or "")
    else:
        raw = l.get("participant_names_raw") or []
        if isinstance(raw, list) and raw:
            student_name = str(raw[0] or "")

    duration_minutes = l.get("duration_minutes")
    duration_str = "未说明"
    if isinstance(duration_minutes, int) and duration_minutes > 0:
        if duration_minutes % 60 == 0:
            duration_str = f"{duration_minutes // 60}小时"
        else:
            duration_str = f"{duration_minutes}分钟"

    topics = l.get("topics") or []
    training_str = "未说明"
    if isinstance(topics, list) and topics:
        training_str = "、".join([str(x) for x in topics if str(x)])

    # Prefer explicit per-lesson class_type.
    # Hard rule: if student isn't resolved (no participant_ids) OR it's a draft, do not show an inferred default.
    # This ensures the edit page shows "请选择" instead of silently defaulting to 1:1.
    lt = lt_map.get(str(l.get("lesson_type_id") or ""), {})
    has_student = isinstance(participant_ids, list) and len(participant_ids) > 0
    if (not has_student) or bool(l.get("draft")):
        class_type = str(l.get("class_type") or "")
    else:
        class_type = str(l.get("class_type") or lt.get("code") or "")

    return {
        "id": l.get("id"),
        "date": l.get("start_at") or l.get("created_at") or "",
        "student": student_name,
        "duration": duration_str,
        "training": training_str,
        "status": l.get("status") or "未说明",
        "next_plan_time": l.get("next_plan_time"),
        # allow empty next_plan (e.g., when note only specifies a next date)
        "next_plan": str(l.get("next_plan") or ""),
        "class_type": class_type,
        "original_text": l.get("original_text") or "",
        "venue": l.get("venue") or "",
    }


def _student_paid(db: dict[str, Any], student_id: str) -> bool:
    payments = db.get("payments", [])
    if not isinstance(payments, list):
        return False
    for p in payments:
        if isinstance(p, dict) and p.get("student_id") == student_id:
            return True
    return False


def _student_by_name(db: dict[str, Any], name: str) -> dict[str, Any] | None:
    for s in db.get("students", []):
        if isinstance(s, dict) and str(s.get("name") or "") == name:
            return s
    return None


def get_student_or_404(db: dict[str, Any], name: str) -> dict[str, Any]:
    student = _student_by_name(db, name)
    if not student:
        abort(404, description=t("error_student_not_found"))
    return student


def _lesson_start_key(l: dict[str, Any]) -> str:
    return str(l.get("start_at") or l.get("created_at") or "")


def _get_openai_client():
    if load_dotenv:
        load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or not OpenAI:
        return None
    return OpenAI(api_key=api_key)


_openai_client = None


def _openai() -> Any:
    global _openai_client
    if _openai_client is None:
        _openai_client = _get_openai_client()
    return _openai_client


def _ai_enabled() -> bool:
    if load_dotenv:
        load_dotenv()
    return bool(os.getenv("OPENAI_API_KEY")) and OpenAI is not None


def _split_next_plan(text: str) -> tuple[str | None, str | None, str]:
    """
    Returns (next_plan_time, next_plan_content, before_text).
    If no next-plan marker is found, (None, None, original_text).
    """
    markers = ("下次", "下周", "下回")
    for marker in markers:
        idx = text.find(marker)
        if idx == -1:
            continue
        before = text[:idx]
        plan = text[idx + len(marker) :].strip()
        plan = plan.lstrip("：:，,。.；; ").strip()

        next_plan_time: str | None = marker
        # If the plan starts with a date-like token, split it out as time.
        plan2 = re.sub(r"^(是|在|于)\s*", "", plan).strip()
        # allow prefixes like "排课在5月12号/安排在5/12/约在..."
        m = re.match(
            r"^(?:排课|排|安排|约|预约|再约|排在|排课在|安排在)?\s*(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}|\d{1,2}月\d{1,2}[号日])\s*",
            plan2,
        )
        if m:
            raw_time = m.group(1)
            # normalize common date formats to YYYY-MM-DD (assume current year if missing)
            try:
                now_y = datetime.now().year
                if re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", raw_time):
                    dt = datetime.strptime(raw_time, "%Y-%m-%d")
                    next_plan_time = dt.strftime("%Y-%m-%d")
                elif re.match(r"^\d{1,2}[/-]\d{1,2}$", raw_time):
                    mm, dd = re.split(r"[/-]", raw_time)
                    dt = datetime(now_y, int(mm), int(dd))
                    next_plan_time = dt.strftime("%Y-%m-%d")
                else:
                    m2 = re.match(r"^(\d{1,2})月(\d{1,2})[号日]$", raw_time)
                    if m2:
                        dt = datetime(now_y, int(m2.group(1)), int(m2.group(2)))
                        next_plan_time = dt.strftime("%Y-%m-%d")
                    else:
                        next_plan_time = raw_time
            except Exception:
                next_plan_time = raw_time

            plan2 = plan2[m.end() :].lstrip("：:，,。.；; ").strip()
            plan = plan2

        # If we only captured a date and no content, keep next_plan empty.
        if not plan:
            return next_plan_time, "", before

        return next_plan_time, (plan or "未说明"), before

    return None, None, text


def _normalize_next_plan_time_input(raw: str) -> str:
    """
    Normalize next_plan_time inputs like:
    - 2026-05-12
    - 5/12 or 5-12
    - 5月12号 / 5月12日
    - 下周三 / 周三
    Returns YYYY-MM-DD if it can be resolved; otherwise returns the trimmed original string.
    """
    s = str(raw or "").strip()
    if not s:
        return ""

    now = datetime.now()
    now_y = now.year

    # YYYY-MM-DD
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", s):
        try:
            dt = datetime.strptime(s, "%Y-%m-%d")
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return s

    # MM/DD or MM-DD
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})", s)
    if m:
        try:
            dt = datetime(now_y, int(m.group(1)), int(m.group(2)))
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return s

    # X月Y号/日
    m = re.fullmatch(r"(\d{1,2})月(\d{1,2})[号日]", s)
    if m:
        try:
            dt = datetime(now_y, int(m.group(1)), int(m.group(2)))
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return s

    # 下周三 / 周三
    m = re.fullmatch(r"(下周)?周?([一二三四五六日天])", s)
    if m:
        is_next_week = bool(m.group(1))
        ch = m.group(2)
        weekday_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
        target = weekday_map.get(ch)
        if not target:
            return s
        try:
            # Monday of current week
            monday = now.date() - timedelta(days=(now.isoweekday() - 1))
            if is_next_week:
                d = monday + timedelta(days=7 + (target - 1))
            else:
                # upcoming weekday in this week; if passed, roll to next week
                d = monday + timedelta(days=(target - 1))
                if d < now.date():
                    d = d + timedelta(days=7)
            return d.strftime("%Y-%m-%d")
        except Exception:
            return s

    return s


def _extract_training_from_practice_clause(text: str) -> str | None:
    """
    Tries to extract the "what we practiced" part from phrases like "练了...".
    Returns the extracted substring or None.
    """
    m = re.search(r"练了([^，。；;\n\r]*)", text)
    if not m:
        return None
    clause = m.group(1).strip()
    if not clause:
        return None
    return clause


def _normalize_student_name_candidate(name: str, known_students: list[str] | None = None) -> str:
    """
    Best-effort cleanup for names extracted from free text.
    - trims common trailing verbs/phrases like "上课/训练/练了..."
    - if known_students provided, maps to canonical DB name when possible
    """
    n = (name or "").strip()
    if not n:
        return n

    # Trim at common boundary words
    # e.g. "萱萱上课两小" -> "萱萱"
    m = re.match(r"^(.+?)(?=(上课|上了课|上了|训练|訓練|练|练了|練|打球|打|$))", n)
    if m:
        n = m.group(1).strip() or n

    # If extracted string is still long, trim at first punctuation/space
    n = re.split(r"[\s，。,.；;：:]", n, maxsplit=1)[0].strip() or n

    # Reject obvious non-name tokens accidentally captured from phrases like "今天上课两小时..."
    bad_tokens = (
        "上课",
        "上了课",
        "训练",
        "訓練",
        "练",
        "练了",
        "練",
        "小时",
        "分钟",
        "下次",
        "下周",
        "下回",
        "今天",
        "昨天",
        "明天",
    )
    if any(tok in n for tok in bad_tokens):
        return ""

    if known_students:
        low = n.casefold()
        # exact (case-insensitive)
        for s in known_students:
            if s and s.casefold() == low:
                return s
        # contained match (prefer longest)
        matches = [s for s in known_students if s and s.casefold() in low]
        if matches:
            matches.sort(key=len, reverse=True)
            return matches[0]

    return n


def _extract_known_students_in_text(text: str, known_students: list[str] | None) -> list[str]:
    """
    Returns all known student names that appear in text (case-insensitive), de-duped.
    Preserves DB canonical names; sorted by first appearance in text.
    """
    if not known_students:
        return []
    low = (text or "").casefold()
    hits: list[tuple[int, str]] = []
    for name in known_students:
        if not name:
            continue
        idx = low.find(name.casefold())
        if idx != -1:
            hits.append((idx, name))
    hits.sort(key=lambda x: x[0])
    out: list[str] = []
    seen = set()
    for _, name in hits:
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _extract_name_list_from_text(text: str) -> list[str]:
    """
    Extracts a list of potential student names from patterns like:
    - 今天Emily, Uma上了课...
    - 今天 Emily 和 Uma 上课...
    Returns 0..N names (as raw tokens, not validated against DB).
    """
    t = (text or "").strip()
    if not t:
        return []
    # Try "今天 <names> 上课/上了课/练/训练"
    m = re.search(r"今天\s*([^。；;\n\r]*)", t)
    candidate = ""
    if m:
        candidate = m.group(1)
    else:
        candidate = t

    # Stop at the first action word if present
    candidate = re.split(r"(上课|上了课|上了|训练|訓練|练|练了|練)", candidate, maxsplit=1)[0]
    candidate = candidate.strip(" ：:，,。；;").strip()
    if not candidate:
        return []

    parts = [p.strip() for p in re.split(r"[、,，/和&]+", candidate) if p.strip()]
    # filter to reasonable name tokens
    names: list[str] = []
    for p in parts:
        if re.fullmatch(r"[A-Za-z]{2,20}", p) or re.fullmatch(r"[\u4e00-\u9fff]{1,10}", p):
            names.append(p)
    # de-dupe preserve order
    out: list[str] = []
    seen = set()
    for n in names:
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def _parse_lesson_text_rule_based(text: str, known_students: list[str] | None = None) -> dict[str, Any]:
    lesson: dict[str, Any] = {
        "student": "未知",
        "students": [],
        "duration": "未说明",
        "training": "未说明",
        "status": "未说明",
        "next_plan": "未说明",
        "original_text": text,
        "date": _now_minute(),
    }

    # Student (1) match existing students (case-insensitive, prefer longest name)
    if known_students:
        matches = _extract_known_students_in_text(text, known_students)
        if matches:
            lesson["students"] = matches
            # keep backward-compatible single-student field
            lesson["student"] = matches[0]

    # Student (1b) if not enough matches, try extracting multiple raw names from text
    if (not lesson.get("students")) or (isinstance(lesson.get("students"), list) and len(lesson["students"]) < 2):
        raw_names = _extract_name_list_from_text(text)
        if len(raw_names) >= 2:
            cleaned = [_normalize_student_name_candidate(n, known_students=known_students) for n in raw_names]
            cleaned = [n for n in cleaned if n and n not in ("未知", "未说明")]
            if len(cleaned) >= 2:
                lesson["students"] = cleaned
                lesson["student"] = cleaned[0]

    # Student (2) fallback simple keywords
    if lesson["student"] == "未知":
        if "小王" in text:
            lesson["student"] = "小王"
        elif "小李" in text:
            lesson["student"] = "小李"
        elif "小张" in text:
            lesson["student"] = "小张"

    # Student (3) fallback: extract a likely name after "今天"
    if lesson["student"] == "未知":
        m = re.search(
            r"今天\s*([A-Za-z]{2,20}|[\u4e00-\u9fff]{1,10}?)(?=(上课|上了|训练|訓練|练|練|\s|[，。,.；;：:]|$))",
            text,
        )
        if m:
            lesson["student"] = _normalize_student_name_candidate(m.group(1), known_students=known_students)
            lesson["students"] = [lesson["student"]] if lesson["student"] != "未知" else []

    # Student (4) fallback: name at beginning, like "萱萱 今天上课..."
    if lesson["student"] == "未知":
        m = re.match(
            r"^\s*([A-Za-z]{2,20}|[\u4e00-\u9fff]{1,10}?)(?=(\s*今天)?\s*(上课|上了|训练|訓練|练|練))",
            text,
        )
        if m:
            lesson["student"] = _normalize_student_name_candidate(m.group(1), known_students=known_students)
            lesson["students"] = [lesson["student"]] if lesson["student"] != "未知" else []

    next_plan_time, next_plan_content, before_plan_text = _split_next_plan(text)

    # Duration
    if "1小时" in text or "一小时" in text:
        lesson["duration"] = "1小时"
    elif "半小时" in text:
        lesson["duration"] = "0.5小时"
    elif "2小时" in text or "两小时" in text:
        lesson["duration"] = "2小时"
    else:
        m = re.search(r"(\d{1,3})\s*分钟", text)
        if m:
            lesson["duration"] = f"{m.group(1)}分钟"

    # Training items
    trainings: list[str] = []
    training_source = _extract_training_from_practice_clause(before_plan_text) or before_plan_text
    for item in ["发球", "步伐", "接球", "反手", "正手", "体能", "拉球", "高远球", "启动", "移动"]:
        if item in training_source:
            trainings.append(item)
    if trainings:
        lesson["training"] = "、".join(trainings)

    # Status
    if "状态不错" in text or "表现不错" in text:
        lesson["status"] = "状态不错"
    elif "累" in text:
        lesson["status"] = "比较累"
    elif "进步" in text:
        lesson["status"] = "有进步"

    # Next plan
    if next_plan_time or next_plan_content is not None:
        # if we captured a date-only next plan, allow empty content
        lesson["next_plan_time"] = next_plan_time or "未说明"
        lesson["next_plan"] = "" if next_plan_content in (None, "未说明") else str(next_plan_content)

    return lesson


def parse_lesson_text(text: str, known_students: list[str] | None = None) -> dict[str, Any]:
    """
    AI-first parsing (OpenAI). Falls back to rule-based parsing if:
    - dependencies are missing
    - OPENAI_API_KEY is not set
    - the API call fails
    - the response isn't valid JSON
    """
    client = _openai()
    if not client:
        return _parse_lesson_text_rule_based(text, known_students=known_students)

    student_hint = ""
    if known_students:
        student_hint = f"\nKnown students (case-insensitive match): {', '.join(known_students[:50])}"

    system_prompt = f"""
You are a coach lesson note parser.
Convert the user's one-sentence lesson note into JSON.

Return JSON only. No explanation.

JSON format:
{{
  "student": "student name",
  "duration": "lesson duration",
  "training": "training focus",
  "status": "student status",
  "next_plan": "next plan"
}}

If a field is not mentioned, use "未说明".
{student_hint}
""".strip()

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
        )

        result = response.choices[0].message.content or "{}"
        lesson = json.loads(result)

        if not isinstance(lesson, dict):
            raise ValueError("AI response is not a JSON object")

        # Normalize & fill missing fields
        def _get(key: str) -> str:
            v = lesson.get(key)
            if v is None:
                return "未说明"
            if isinstance(v, (int, float)):
                return str(v)
            if not isinstance(v, str):
                return "未说明"
            v = v.strip()
            return v if v else "未说明"

        parsed: dict[str, Any] = {
            "student": _get("student"),
            "students": [],
            "duration": _get("duration"),
            "training": _get("training"),
            "status": _get("status"),
            "next_plan": _get("next_plan"),
            "original_text": text,
            "date": _now_minute(),
        }

        # If note says "今天", do not let AI move the lesson date to a future date (e.g. next plan date).
        if "今天" in text:
            parsed["date"] = _now_minute()

        # If AI returns a student name that differs only by case, map to DB canonical name
        if known_students and isinstance(parsed.get("student"), str) and parsed["student"] != "未说明":
            parsed["student"] = _normalize_student_name_candidate(parsed["student"], known_students=known_students)
        elif isinstance(parsed.get("student"), str):
            parsed["student"] = _normalize_student_name_candidate(parsed["student"], known_students=known_students)

        # Multi-student: prefer matching from known_students found in text
        matches = _extract_known_students_in_text(text, known_students)
        if len(matches) >= 2:
            parsed["students"] = matches
            parsed["student"] = matches[0]
        elif parsed.get("student") not in ("未说明", "", None):
            # If AI returned "Emily, Uma" style, split and map to known names when possible
            raw = str(parsed.get("student") or "").strip()
            parts = [p.strip() for p in re.split(r"[、,，/和&]+", raw) if p.strip()]
            mapped: list[str] = []
            for p in parts:
                cand = _normalize_student_name_candidate(p, known_students=known_students)
                if cand and cand != "未知" and cand != "未说明":
                    mapped.append(cand)
            # de-dupe preserve order
            dedup = []
            seen = set()
            for n in mapped:
                if n in seen:
                    continue
                seen.add(n)
                dedup.append(n)
            if len(dedup) >= 2:
                parsed["students"] = dedup
                parsed["student"] = dedup[0]
            elif len(dedup) == 1:
                parsed["students"] = dedup

        # If still not multi-student, try extracting raw names from text (supports new names)
        if not (isinstance(parsed.get("students"), list) and len(parsed["students"]) >= 2):
            raw_names = _extract_name_list_from_text(text)
            if len(raw_names) >= 2:
                cleaned = [_normalize_student_name_candidate(n, known_students=known_students) for n in raw_names]
                cleaned = [n for n in cleaned if n and n not in ("未知", "未说明")]
                # de-dupe
                out = []
                seen = set()
                for n in cleaned:
                    if n in seen:
                        continue
                    seen.add(n)
                    out.append(n)
                if len(out) >= 2:
                    parsed["students"] = out
                    parsed["student"] = out[0]

        # Always derive next_plan_time/content from the original text if present
        np_time, np_content, before = _split_next_plan(text)
        if np_time or np_content is not None:
            # If we captured a date-only next plan, keep content empty.
            if np_time:
                parsed["next_plan_time"] = np_time
            # Prefer marker-based plan if AI didn't provide one.
            if parsed.get("next_plan") in ("未说明", "", None):
                parsed["next_plan"] = "" if (np_content in (None, "未说明", "")) else str(np_content)

        # If there's a "练了..." clause, use it to avoid mixing next-plan keywords into training.
        practice_clause = _extract_training_from_practice_clause(before)
        if practice_clause:
            trainings: list[str] = []
            for item in ["发球", "步伐", "接球", "反手", "正手", "体能", "拉球", "高远球", "启动", "移动"]:
                if item in practice_clause:
                    trainings.append(item)
            if trainings:
                parsed["training"] = "、".join(trainings)

        return parsed

    except Exception:
        return _parse_lesson_text_rule_based(text, known_students=known_students)


def load_db() -> dict[str, Any]:
    if DB_FILE.exists():
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if _is_legacy_db(data):
            migrated = _migrate_v0_to_v1(data)
            _clean_db(migrated)
            save_db(migrated)
            return migrated

        data = _ensure_v1_shape(data)
        if _clean_db(data):
            save_db(data)
        return data

    return _ensure_v1_shape({"schema_version": SCHEMA_VERSION})


def save_db(db: dict[str, Any]) -> None:
    tmp = DB_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, DB_FILE)


def _price_per_hour(db: dict[str, Any], class_type: str) -> float:
    for lt in db.get("lesson_types", []):
        if isinstance(lt, dict) and str(lt.get("code") or "") == class_type:
            try:
                return float(lt.get("price_per_hour") or 0.0)
            except Exception:
                return 0.0
    return 0.0


def _lesson_fee(db: dict[str, Any], lesson: dict[str, Any]) -> float:
    duration_minutes = lesson.get("duration_minutes")
    hours = (float(duration_minutes) / 60.0) if isinstance(duration_minutes, int) and duration_minutes > 0 else 0.0

    # Prefer per-lesson snapshot pricing (per person per 2h lesson baseline).
    ppl = None
    try:
        if lesson.get("price_per_lesson") is not None:
            ppl = float(lesson.get("price_per_lesson"))
    except Exception:
        ppl = None

    if ppl is None:
        lt_id = str(lesson.get("lesson_type_id") or "")
        for lt in db.get("lesson_types", []):
            if isinstance(lt, dict) and str(lt.get("id") or "") == lt_id:
                try:
                    ppl = float(lt.get("price_per_lesson") or 0.0)
                except Exception:
                    ppl = 0.0
                break
        if ppl is None:
            ppl = 0.0

    baseline = lesson.get("baseline_minutes")
    if not (isinstance(baseline, int) and baseline > 0):
        baseline = _baseline_minutes_for_class_type(str(lesson.get("class_type") or ""))
    # Scale by actual duration vs baseline
    per_person_fee = ppl * (hours / (float(baseline) / 60.0)) if hours > 0 else 0.0

    pids = lesson.get("participant_ids") or []
    participants = len(pids) if isinstance(pids, list) else 0
    participants = participants if participants > 0 else 0
    return per_person_fee * float(participants)


def _lesson_student_fee_map(db: dict[str, Any], lesson: dict[str, Any]) -> dict[str, float]:
    """
    Allocate a lesson's total fee across participants equally.
    This avoids double-counting revenue when generating per-student rankings.
    """
    fee = _lesson_fee(db, lesson)
    pids = lesson.get("participant_ids") or []
    if not isinstance(pids, list) or not pids:
        return {}
    share = fee / float(len(pids)) if len(pids) > 0 else 0.0
    return {str(pid): share for pid in pids if str(pid)}


def _lesson_per_person_fee(db: dict[str, Any], lesson: dict[str, Any]) -> tuple[float, str]:
    """Returns (amount_per_person, currency)"""
    duration_minutes = lesson.get("duration_minutes")
    hours = (float(duration_minutes) / 60.0) if isinstance(duration_minutes, int) and duration_minutes > 0 else 0.0
    currency = str(lesson.get("currency") or "")

    ppl = None
    try:
        ppl = float(lesson.get("price_per_lesson")) if lesson.get("price_per_lesson") is not None else None
    except Exception:
        ppl = None

    if ppl is None or not currency:
        lt_id = str(lesson.get("lesson_type_id") or "")
        if not currency:
            currency = "CNY"
        for lt in db.get("lesson_types", []):
            if isinstance(lt, dict) and str(lt.get("id") or "") == lt_id:
                currency = str(lt.get("currency") or currency)
                try:
                    ppl = float(lt.get("price_per_lesson") or 0.0) if ppl is None else ppl
                except Exception:
                    ppl = 0.0
                break
        if ppl is None:
            ppl = 0.0

    baseline = lesson.get("baseline_minutes")
    if not (isinstance(baseline, int) and baseline > 0):
        baseline = _baseline_minutes_for_class_type(str(lesson.get("class_type") or ""))
    per_person_fee = ppl * (hours / (float(baseline) / 60.0)) if hours > 0 else 0.0
    return per_person_fee, currency


def _lesson_participant_names(db: dict[str, Any], lesson: dict[str, Any]) -> list[str]:
    sid_to_name = {str(s.get("id")): str(s.get("name")) for s in db.get("students", []) if isinstance(s, dict)}
    pids = lesson.get("participant_ids") or []
    names: list[str] = []
    if isinstance(pids, list) and pids:
        for sid in pids:
            sid = str(sid)
            if sid and sid in sid_to_name:
                names.append(sid_to_name[sid])
    if names:
        return names
    raw = lesson.get("participant_names_raw") or []
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    return []


def _venues(db: dict[str, Any]) -> list[str]:
    coaches = db.get("coaches", [])
    if isinstance(coaches, list) and coaches and isinstance(coaches[0], dict):
        venues = coaches[0].get("venues")
        if isinstance(venues, list):
            return [str(v).strip() for v in venues if str(v).strip()]
    return []


def _payment_matches_lesson(p: dict[str, Any], lesson_id: str, student_id: str) -> bool:
    if str(p.get("student_id") or "") != student_id:
        return False
    if str(p.get("lesson_id") or "") == lesson_id:
        return True
    lesson_ids = p.get("lesson_ids")
    if isinstance(lesson_ids, list) and lesson_id in [str(x) for x in lesson_ids]:
        return True
    return False


def _lesson_paid_status(db: dict[str, Any], lesson: dict[str, Any]) -> tuple[int, int]:
    """returns (paid_count, total_participants)"""
    pids = lesson.get("participant_ids") or []
    if not isinstance(pids, list):
        return 0, 0
    total = len(pids)
    if total == 0:
        return 0, 0
    payments = db.get("payments", [])
    if not isinstance(payments, list):
        return 0, total
    lid = str(lesson.get("id") or "")
    paid = 0
    for sid in pids:
        sid = str(sid)
        if any(isinstance(p, dict) and _payment_matches_lesson(p, lid, sid) for p in payments):
            paid += 1
    return paid, total


def _lesson_is_fully_paid(db: dict[str, Any], lesson: dict[str, Any]) -> bool:
    paid, total = _lesson_paid_status(db, lesson)
    return total > 0 and paid == total


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-me")


def get_lesson_or_404(db: dict[str, Any], lesson_id: str) -> dict[str, Any]:
    lessons = db.get("lessons", [])
    if not isinstance(lessons, list):
        abort(404, description="Lesson not found")
    for l in lessons:
        if isinstance(l, dict) and str(l.get("id")) == lesson_id:
            return l
    abort(404, description="Lesson not found")


def get_lang() -> str:
    lang = (session.get("lang") or "").strip().lower()
    if lang in SUPPORTED_LANGS:
        return lang
    return "en"


@app.before_request
def _capture_lang_selection():
    lang = (request.args.get("lang") or "").strip().lower()
    if lang in SUPPORTED_LANGS:
        session["lang"] = lang


def t(key: str) -> str:
    lang = get_lang()
    return TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(key, TRANSLATIONS["en"].get(key, key))


@app.context_processor
def inject_i18n():
    return {"t": t, "current_lang": get_lang(), "ai_enabled": _ai_enabled()}


@app.get("/")
def index():
    db = load_db()
    today = datetime.now().strftime("%Y-%m-%d")
    lessons = _lessons(db)
    todays = []
    for l in lessons:
        start_at = str(l.get("start_at") or "")
        if start_at.startswith(today):
            view = _lesson_view(db, l)
            try:
                view["total_fee"] = float(_lesson_fee(db, l))
            except Exception:
                view["total_fee"] = 0.0
            view["currency"] = str(l.get("currency") or "SGD")
            paid_count, total = _lesson_paid_status(db, l)
            view["paid_count"] = paid_count
            view["paid_total"] = total
            view["is_paid"] = (total > 0 and paid_count == total)
            todays.append(view)
    todays.sort(key=lambda v: str(v.get("date") or ""), reverse=False)

    return render_template("index.html", todays_lessons=todays)


@app.get("/students/hub")
def students_hub():
    db = load_db()
    q = (request.args.get("q") or "").strip()
    tab = (request.args.get("tab") or "active").strip()  # active|inactive
    sort_key = (request.args.get("sort") or "recent").strip()  # recent|count

    # build last lesson + counts
    last_by_sid: dict[str, datetime] = {}
    count_by_sid: dict[str, int] = {}
    now = datetime.now()
    for l in _lessons(db):
        if not isinstance(l, dict):
            continue
        if _is_draft_lesson(l):
            continue
        dt = _parse_dt(str(l.get("start_at") or "")) or None
        pids = l.get("participant_ids") or []
        if not isinstance(pids, list) or not dt:
            continue
        for sid in pids:
            sid = str(sid)
            if not sid:
                continue
            count_by_sid[sid] = count_by_sid.get(sid, 0) + 1
            prev = last_by_sid.get(sid)
            if not prev or dt > prev:
                last_by_sid[sid] = dt

    def had_within(sid: str, days: int) -> bool:
        last = last_by_sid.get(sid)
        if not last:
            return False
        return (now.date() - last.date()).days <= days

    rows = []
    for s in db.get("students", []):
        if not isinstance(s, dict) or not s.get("id") or not s.get("name"):
            continue
        sid = str(s["id"])
        name = str(s["name"])
        last = last_by_sid.get(sid)
        days_since = (now.date() - last.date()).days if last else None
        manual_inactive = bool(s.get("archived"))
        auto_inactive = (days_since is not None and days_since >= 60)
        is_inactive = manual_inactive or auto_inactive

        if tab == "inactive" and not is_inactive:
            continue
        if tab != "inactive" and is_inactive:
            continue

        if q:
            qf = q.casefold()
            contact = str(s.get("contact") or "")
            if qf not in name.casefold() and (not contact or qf not in contact.casefold()):
                continue

        rows.append(
            {
                "id": sid,
                "name": name,
                "contact": str(s.get("contact") or ""),
                "last_at": last.strftime("%Y-%m-%d %H:%M") if last else "—",
                "last_dt": last,
                "count": int(count_by_sid.get(sid, 0)),
                "has_10": had_within(sid, 10),
                "has_20": had_within(sid, 20),
                "has_30": had_within(sid, 30),
                "inactive": is_inactive,
                "inactive_days": days_since,
            }
        )

    if sort_key == "count":
        rows.sort(key=lambda r: (r["count"], r["last_dt"] or datetime.min), reverse=True)
    else:
        rows.sort(key=lambda r: (r["last_dt"] or datetime.min, r["count"]), reverse=True)

    return render_template("students_hub.html", q=q, tab=tab, sort=sort_key, rows=rows)

@app.route("/students/<student_id>/edit", methods=["GET", "POST"])
def edit_student(student_id: str):
    db = load_db()
    student = None
    for s in db.get("students", []):
        if isinstance(s, dict) and str(s.get("id") or "") == student_id:
            student = s
            break
    if not student:
        abort(404, description="Student not found")

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        default_class_type = _normalize_class_type_code((request.form.get("default_class_type") or "").strip()) or "1:1"
        contact = (request.form.get("contact") or "").strip()
        note = (request.form.get("note") or "").strip()
        venue = (request.form.get("venue") or "").strip()
        if not name:
            return render_template("student_edit.html", student=student)

        # prevent duplicate names by merging (simple)
        existing = _student_by_name(db, name)
        if existing and existing is not student:
            # merge: move references to existing id, then drop this student
            old_id = str(student.get("id"))
            new_id = str(existing.get("id"))
            for l in db.get("lessons", []):
                if isinstance(l, dict) and isinstance(l.get("participant_ids"), list):
                    l["participant_ids"] = [new_id if str(x) == old_id else str(x) for x in l["participant_ids"]]
            for p in db.get("payments", []):
                if isinstance(p, dict) and str(p.get("student_id") or "") == old_id:
                    p["student_id"] = new_id
            db["students"] = [s for s in db["students"] if not (isinstance(s, dict) and str(s.get("id") or "") == old_id)]
            existing["contact"] = contact
            existing["note"] = note
            existing["venue"] = venue
            existing["default_class_type"] = default_class_type
        else:
            student["name"] = name
            student["contact"] = contact
            student["note"] = note
            student["venue"] = venue
            student["default_class_type"] = default_class_type

        save_db(db)
        return redirect(url_for("students_hub"))

    venues = _venues(db)
    view = {
        "id": str(student.get("id") or ""),
        "name": str(student.get("name") or ""),
        "contact": str(student.get("contact") or ""),
        "note": str(student.get("note") or ""),
        "venue": str(student.get("venue") or ""),
        "default_class_type": _normalize_class_type_code(str(student.get("default_class_type") or "")) or "1:1",
    }
    return render_template("student_edit.html", student=view, venues=venues)


@app.get("/students")
def student_search():
    db = load_db()
    q = (request.args.get("q") or "").strip()
    students_rows = []
    for s in db.get("students", []):
        if isinstance(s, dict) and s.get("id") and s.get("name"):
            students_rows.append(
                (
                    str(s["name"]),
                    {
                        "phone": s.get("contact") or "",
                        "paid": _student_paid(db, str(s["id"])),
                        "created_at": s.get("created_at") or "",
                    },
                )
            )
    students_rows.sort(key=lambda x: x[0])

    results = students_rows
    if q:
        qf = q.casefold()
        filtered = []
        for name, s in students_rows:
            phone = str((s or {}).get("phone") or "")
            if qf in name.casefold() or (phone and qf in phone.casefold()):
                filtered.append((name, s))
        results = filtered

    return render_template("student_search.html", q=q, results=results, total=len(students_rows))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    db = load_db()
    lesson_types = [lt for lt in db.get("lesson_types", []) if isinstance(lt, dict)]
    lesson_types.sort(key=lambda lt: str(lt.get("code") or ""))
    venues = _venues(db)

    if request.method == "POST":
        for lt in lesson_types:
            code = str(lt.get("code") or "")
            if not code:
                continue
            raw = (request.form.get(f"price_{code}") or "").strip()
            try:
                # UI input is per-lesson per-person price (baseline depends on class type)
                lt["price_per_lesson"] = float(raw) if raw else 0.0
                # keep derived hourly for backward compatibility
                baseline = _baseline_minutes_for_class_type(code)
                lt["price_per_hour"] = float(lt["price_per_lesson"]) / (baseline / 60.0)
            except Exception:
                lt["price_per_lesson"] = 0.0
                lt["price_per_hour"] = 0.0

        # venues config (newline separated)
        venues_raw = (request.form.get("venues") or "").strip()
        venues_list = [v.strip() for v in venues_raw.splitlines() if v.strip()]
        if isinstance(db.get("coaches"), list) and db["coaches"] and isinstance(db["coaches"][0], dict):
            db["coaches"][0]["venues"] = venues_list

        save_db(db)
        return redirect(url_for("settings"))

    ordered = [(str(lt.get("code") or ""), float(lt.get("price_per_lesson") or 0.0)) for lt in lesson_types]
    return render_template("settings.html", class_types=ordered, venues="\n".join(venues))


@app.post("/settings/language")
def set_language():
    lang = (request.form.get("lang") or "").strip().lower()
    if lang in SUPPORTED_LANGS:
        session["lang"] = lang
    return redirect(request.referrer or url_for("settings"))


@app.post("/venues/add")
def add_venue():
    db = load_db()
    venue = ""
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        venue = str(payload.get("venue") or "").strip()
    else:
        venue = (request.form.get("venue") or "").strip()

    if not venue:
        return {"ok": False, "error": "missing venue"}, 400

    coaches = db.get("coaches", [])
    if not (isinstance(coaches, list) and coaches and isinstance(coaches[0], dict)):
        return {"ok": False, "error": "no coach"}, 500

    venues = coaches[0].get("venues")
    if not isinstance(venues, list):
        venues = []
        coaches[0]["venues"] = venues

    # de-dupe (case-insensitive)
    low = venue.casefold()
    if not any(str(v).strip().casefold() == low for v in venues):
        venues.append(venue)
        save_db(db)

    return {"ok": True, "venue": venue, "venues": venues}


@app.get("/stats")
def stats():
    db = load_db()
    range_key = (request.args.get("range") or "month").strip()
    start = request.args.get("start")
    end = request.args.get("end")
    leaderboard_month = (request.args.get("month") or datetime.now().strftime("%Y-%m")).strip()

    start_dt, end_dt = _range_to_start_end(range_key, start, end)
    lessons = _lessons(db)
    lessons = [l for l in lessons if _lesson_in_range({"date": l.get("start_at")}, start_dt, end_dt)]

    totals = {"fee": 0.0, "hours": 0.0, "lessons": 0, "students": 0}
    students_set: set[str] = set()
    per_month: dict[str, dict[str, Any]] = {}

    # Today stats
    today_key = datetime.now().strftime("%Y-%m-%d")
    today_fee = 0.0
    today_hours = 0.0

    for l in lessons:
        dt = _parse_dt(l.get("start_at")) or datetime.now()
        mk = _month_key(dt)
        per_month.setdefault(mk, {"month": mk, "fee": 0.0, "hours": 0.0, "lessons": 0, "students": set()})

        fee = _lesson_fee(db, l)
        duration_minutes = l.get("duration_minutes")
        hours = (float(duration_minutes) / 60.0) if isinstance(duration_minutes, int) and duration_minutes > 0 else 0.0

        # Count distinct students by participant ids; if none, ignore for student count
        pids = l.get("participant_ids") or []
        if isinstance(pids, list):
            for sid in pids:
                sid = str(sid)
                if sid:
                    students_set.add(sid)
                    per_month[mk]["students"].add(sid)

        totals["fee"] += fee
        totals["hours"] += hours
        totals["lessons"] += 1

        per_month[mk]["fee"] += fee
        per_month[mk]["hours"] += hours
        per_month[mk]["lessons"] += 1

        if dt.strftime("%Y-%m-%d") == today_key:
            today_fee += fee
            today_hours += hours

    totals["students"] = len(students_set)

    rows = sorted(per_month.values(), key=lambda r: r["month"])
    # Convert month student sets to counts
    for r in rows:
        r["students"] = len(r["students"])

    # Most active students by hours (Top 5) for a given month
    sid_to_name = {str(s.get("id")): str(s.get("name")) for s in db.get("students", []) if isinstance(s, dict)}
    hours_by_sid: dict[str, float] = {}
    for l in _lessons(db):
        if not isinstance(l, dict):
            continue
        dt = _parse_dt(l.get("start_at"))
        if not dt or _month_key(dt) != leaderboard_month:
            continue
        duration_minutes = l.get("duration_minutes")
        hours = (float(duration_minutes) / 60.0) if isinstance(duration_minutes, int) and duration_minutes > 0 else 0.0
        pids = l.get("participant_ids") or []
        if not isinstance(pids, list):
            continue
        for sid in pids:
            sid = str(sid)
            if sid:
                hours_by_sid[sid] = hours_by_sid.get(sid, 0.0) + hours

    most_active = [
        {"student": sid_to_name.get(sid, sid), "hours": h}
        for sid, h in sorted(hours_by_sid.items(), key=lambda kv: kv[1], reverse=True)
        if h > 0
    ][:5]

    # Unpaid list for selected month: show student name + lesson time
    unpaid_rows = []
    payments = db.get("payments", [])
    payments_list = payments if isinstance(payments, list) else []
    for l in _lessons(db):
        if not isinstance(l, dict):
            continue
        dt = _parse_dt(l.get("start_at"))
        if not dt or _month_key(dt) != leaderboard_month:
            continue
        lid = str(l.get("id") or "")
        pids = l.get("participant_ids") or []
        if not isinstance(pids, list) or not pids:
            continue
        for sid in pids:
            sid = str(sid)
            if not sid:
                continue
            if any(isinstance(p, dict) and _payment_matches_lesson(p, lid, sid) for p in payments_list):
                continue
            unpaid_rows.append(
                {
                    "student_id": sid,
                    "student": sid_to_name.get(sid, sid),
                    "time": dt.strftime("%Y-%m-%d %H:%M"),
                    "lesson_id": lid,
                }
            )
    unpaid_rows.sort(key=lambda r: r["time"], reverse=True)

    # Inactive reminders (mutually exclusive buckets by days since last lesson)
    last_by_sid: dict[str, datetime] = {}
    for l in _lessons(db):
        if not isinstance(l, dict):
            continue
        dt = _parse_dt(l.get("start_at"))
        if not dt:
            continue
        pids = l.get("participant_ids") or []
        if not isinstance(pids, list):
            continue
        for sid in pids:
            sid = str(sid)
            if not sid:
                continue
            prev = last_by_sid.get(sid)
            if not prev or dt > prev:
                last_by_sid[sid] = dt

    now = datetime.now()
    # Buckets (mutually exclusive): 7-15, 15-25, 25-35 (displayed as ranges in template)
    # Note: boundary days (15, 25) go to the higher bucket to avoid duplicates.
    buckets = {7: [], 15: [], 25: []}
    for s in db.get("students", []):
        if not isinstance(s, dict) or not s.get("id") or not s.get("name"):
            continue
        sid = str(s["id"])
        last = last_by_sid.get(sid)
        if not last:
            continue
        days = (now.date() - last.date()).days
        if 25 <= days <= 35:
            buckets[25].append({"name": str(s["name"]), "days": days, "last": last.strftime("%Y-%m-%d")})
        elif 15 <= days < 25:
            buckets[15].append({"name": str(s["name"]), "days": days, "last": last.strftime("%Y-%m-%d")})
        elif 7 <= days < 15:
            buckets[7].append({"name": str(s["name"]), "days": days, "last": last.strftime("%Y-%m-%d")})

    # sort each bucket by days desc
    for threshold in buckets:
        buckets[threshold].sort(key=lambda r: r["days"], reverse=True)

    # Month options: recent 6 months (including current)
    month_options = []
    now_m = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for i in range(6):
        m = (now_m - timedelta(days=30 * i))
        month_options.append(_month_key(m))

    return render_template(
        "stats.html",
        range_key=range_key,
        start=start or "",
        end=end or "",
        totals=totals,
        rows=rows,
        today={"fee": today_fee, "hours": today_hours},
        leaderboard_month=leaderboard_month,
        most_active=most_active,
        month_options=month_options,
        unpaid=unpaid_rows,
        inactive=buckets,
    )


@app.get("/stats/monthly-lessons")
def monthly_lessons():
    db = load_db()
    month = (request.args.get("month") or datetime.now().strftime("%Y-%m")).strip()

    lessons = []
    for l in _lessons(db):
        if not isinstance(l, dict):
            continue
        dt = _parse_dt(l.get("start_at"))
        if not dt or _month_key(dt) != month:
            continue
        per_person, currency = _lesson_per_person_fee(db, l)
        total = _lesson_fee(db, l)
        duration_minutes = l.get("duration_minutes") or 0
        hours = (float(duration_minutes) / 60.0) if isinstance(duration_minutes, int) and duration_minutes > 0 else 0.0
        names = _lesson_participant_names(db, l)
        lt_id = str(l.get("lesson_type_id") or "")
        code = ""
        for lt in db.get("lesson_types", []):
            if isinstance(lt, dict) and str(lt.get("id") or "") == lt_id:
                code = str(lt.get("code") or "")
                break

        lessons.append(
            {
                "id": str(l.get("id") or ""),
                "time": str(l.get("start_at") or ""),
                "students": ", ".join(names) if names else "—",
                "class_type": code or "—",
                "venue": str(l.get("venue") or ""),
                "hours": hours,
                "participants": len(l.get("participant_ids") or []) if isinstance(l.get("participant_ids"), list) else 0,
                "per_person_fee": per_person,
                "total_fee": total,
                "currency": currency,
            }
        )

    lessons.sort(key=lambda x: x["time"])
    return render_template("monthly_lessons.html", month=month, lessons=lessons)


@app.route("/students/new", methods=["GET", "POST"])
def add_student():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        note = (request.form.get("note") or "").strip()
        venue = (request.form.get("venue") or "").strip()
        default_class_type = (request.form.get("default_class_type") or "").strip()
        if default_class_type not in ("", "1:1", "1:2", "1:3", "1:4"):
            default_class_type = ""

        if not name:
            return render_template(
                "add_student.html",
                error=t("error_name_required"),
                form={
                    "name": name,
                    "phone": phone,
                    "note": note,
                    "venue": venue,
                    "default_class_type": default_class_type,
                },
            )

        db = load_db()
        existing = _student_by_name(db, name)
        if existing:
            existing["contact"] = phone
            existing["note"] = note
            existing["venue"] = venue
            if default_class_type:
                existing["default_class_type"] = default_class_type
        else:
            sid = _new_id()
            db["students"].append(
                {
                    "id": sid,
                    "name": name,
                    "contact": phone,
                    "note": note,
                    "venue": venue,
                    "default_class_type": default_class_type or "1:1",
                    "created_at": _now_minute(),
                    "archived": False,
                }
            )
        save_db(db)
        next_url = (request.args.get("next") or "").strip()
        if next_url:
            return redirect(next_url)
        return redirect(url_for("student_progress", name=name))

    db = load_db()
    prefill_name = (request.args.get("name") or "").strip()
    return render_template(
        "add_student.html",
        error=None,
        form={"name": prefill_name, "phone": "", "note": "", "venue": "", "default_class_type": "1:1"},
        venues=_venues(db),
    )


@app.route("/lessons/new", methods=["GET", "POST"])
def record_lesson():
    db = load_db()
    student_rows = [
        {
            "id": str(s.get("id") or ""),
            "name": str(s.get("name") or ""),
            "default_class_type": _normalize_class_type_code(str(s.get("default_class_type") or "1:1")) or "1:1",
            "venue": str(s.get("venue") or ""),
        }
        for s in db.get("students", [])
        if isinstance(s, dict) and s.get("id") and s.get("name")
    ]
    student_rows.sort(key=lambda x: x["name"])
    students = [r["name"] for r in student_rows]
    lesson_type_prices = {}
    for lt in db.get("lesson_types", []):
        if not isinstance(lt, dict):
            continue
        code = str(lt.get("code") or "").strip()
        if code not in ("1:1", "1:2", "1:3", "1:4"):
            continue
        try:
            ppl = float(lt.get("price_per_lesson") or 0.0)
        except Exception:
            ppl = 0.0
        lesson_type_prices[code] = {"price_per_lesson": ppl, "currency": str(lt.get("currency") or "SGD")}

    # If we were redirected back from "add student", prefill last pending text
    pending_text = session.get("pending_lesson_text") if isinstance(session.get("pending_lesson_text"), str) else ""
    pending_ct = session.get("pending_class_type") if isinstance(session.get("pending_class_type"), str) else ""
    pending_sid = session.get("pending_student_id") if isinstance(session.get("pending_student_id"), str) else ""

    if request.method == "POST":
        try:
            class_type_override = (request.form.get("class_type") or "").strip()
            if class_type_override not in ("", "1:1", "1:2", "1:3", "1:4"):
                class_type_override = ""
            venue_override = (request.form.get("venue") or "").strip()
            selected_student_id = (request.form.get("student_id") or "").strip()
            if selected_student_id and not any(r["id"] == selected_student_id for r in student_rows):
                selected_student_id = ""

            if (request.form.get("action") or "").strip() == "create_student_then_save":
                lesson_text = (request.form.get("lesson_text") or "").strip()
                name = (request.form.get("student_name") or "").strip()
                contact = (request.form.get("contact") or "").strip()
                note = (request.form.get("note") or "").strip()
                default_class_type = (request.form.get("default_class_type") or "").strip() or "1:1"
                venue = (request.form.get("venue") or "").strip()

                if not lesson_text or not name:
                    return render_template(
                        "add_lesson.html",
                        error=t("error_lesson_text_required"),
                        students=students,
                        form={"lesson_text": lesson_text, "class_type": class_type_override},
                    )

                existing = _student_by_name(db, name)
                if existing:
                    existing["contact"] = contact
                    existing["note"] = note
                    if venue:
                        existing["venue"] = venue
                    existing["default_class_type"] = default_class_type
                else:
                    sid = _new_id()
                    db["students"].append(
                        {
                            "id": sid,
                            "name": name,
                            "contact": contact,
                            "note": note,
                            "venue": venue or "",
                            "default_class_type": default_class_type,
                            "created_at": _now_minute(),
                            "archived": False,
                        }
                    )
                save_db(db)
                # reload students list and continue saving lesson with the same text
                students = sorted(
                    [str(s.get("name")) for s in db.get("students", []) if isinstance(s, dict) and s.get("name")]
                )

            text = (request.form.get("lesson_text") or "").strip()
            if not text:
                return render_template(
                    "add_lesson.html",
                    error=t("error_lesson_text_required"),
                    students=students,
                    student_rows=student_rows,
                    form={"lesson_text": text, "class_type": class_type_override, "student_id": selected_student_id},
                    venues=_venues(db),
                    lesson_type_prices=lesson_type_prices,
                )

            parsed = parse_lesson_text(text, known_students=students)
            parsed_students = parsed.get("students")
            student_names: list[str] = []
            if isinstance(parsed_students, list) and parsed_students:
                student_names = [str(x).strip() for x in parsed_students if str(x).strip()]
            else:
                s1 = str(parsed.get("student") or "").strip()
                student_names = [s1] if s1 and s1 not in ("未知", "未说明") else []
            # Normalize / filter out obvious non-name tokens (e.g., "今天")
            cleaned: list[str] = []
            for nm in student_names:
                nn = _normalize_student_name_candidate(str(nm), known_students=students)
                if nn and nn not in ("未知", "未说明"):
                    cleaned.append(nn)
            # de-dupe while keeping order
            seen = set()
            student_names = []
            for nm in cleaned:
                if nm not in seen:
                    seen.add(nm)
                    student_names.append(nm)
            student_name = student_names[0] if student_names else ""

            # Ensure student exists
            participant_ids: list[str] = []
            unknowns: list[str] = []

            # Manual student selection overrides any parsed names
            if selected_student_id:
                s_obj = next((x for x in db.get("students", []) if isinstance(x, dict) and str(x.get("id")) == selected_student_id), None)
                if isinstance(s_obj, dict) and s_obj.get("name"):
                    participant_ids = [selected_student_id]
                    unknowns = []
                    student_name = str(s_obj.get("name") or "")
                    student_names = [student_name]
                else:
                    selected_student_id = ""

            if student_names and any(n not in ("未知", "未说明", "") for n in student_names):
                for nm in student_names:
                    nm = nm or ""
                    if nm in ("", "未知", "未说明"):
                        continue
                    s = _student_by_name(db, nm)
                    if not s:
                        unknowns.append(nm)
                    else:
                        participant_ids.append(str(s.get("id")))

            if unknowns:
                # If there are also known students, record them first (split into separate lessons).
                # Then redirect to add student for the first unknown name.
                session["pending_lesson_text"] = text
                session["pending_class_type"] = class_type_override
                session["pending_student_id"] = selected_student_id
                if participant_ids:
                    # record known students first
                    duration_minutes = int(round(_duration_to_hours(str(parsed.get("duration") or "")) * 60))
                    duration_minutes = duration_minutes if duration_minutes > 0 else None
                    training = str(parsed.get("training") or "")
                    topics = [t.strip() for t in re.split(r"[、,，/]+", training) if t.strip()] if training else []
                    coach_id = (
                        str((db.get("coaches") or [{}])[0].get("id") or "")
                        if isinstance(db.get("coaches"), list)
                        else ""
                    )
                    for sid in participant_ids:
                        s_obj = next((x for x in db.get("students", []) if isinstance(x, dict) and str(x.get("id")) == str(sid)), None)
                        s_name = str((s_obj or {}).get("name") or "")
                        default_code = str((s_obj or {}).get("default_class_type") or "").strip()
                        chosen_code = class_type_override or default_code or ""
                        lesson_type_id = _lesson_type_id_by_code(db, chosen_code) if chosen_code else None
                        snapshot_ppl = 0.0
                        snapshot_currency = "SGD"
                        for lt in db.get("lesson_types", []):
                            if isinstance(lt, dict) and str(lt.get("id") or "") == str(lesson_type_id or ""):
                                try:
                                    snapshot_ppl = float(lt.get("price_per_lesson") or 0.0)
                                except Exception:
                                    snapshot_ppl = 0.0
                                snapshot_currency = str(lt.get("currency") or snapshot_currency)
                                break
                        venue = venue_override or str(parsed.get("venue") or "") or str((s_obj or {}).get("venue") or "")
                        is_draft = False
                        if not s_name or duration_minutes is None or not chosen_code or not venue or lesson_type_id is None:
                            is_draft = True
                        db["lessons"].append(
                            {
                                "id": _new_id(),
                                "coach_id": coach_id,
                                "lesson_type_id": lesson_type_id,
                                "class_type": chosen_code,
                                "price_per_lesson": snapshot_ppl,
                                "baseline_minutes": _baseline_minutes_for_class_type(chosen_code),
                                "price_per_hour": (
                                    snapshot_ppl / (_baseline_minutes_for_class_type(chosen_code) / 60.0)
                                )
                                if snapshot_ppl
                                else 0.0,
                                "currency": snapshot_currency,
                                "start_at": str(parsed.get("date") or _now_minute()),
                                "duration_minutes": duration_minutes,
                                "participant_ids": [str(sid)],
                                "participant_names_raw": [],
                                "draft": is_draft,
                                "topics": topics,
                                "status": str(parsed.get("status") or "未说明"),
                                "next_plan_time": parsed.get("next_plan_time") or None,
                                "next_plan": "未说明" if parsed.get("next_plan") is None else str(parsed.get("next_plan") or ""),
                                "original_text": str(parsed.get("original_text") or ""),
                                "venue": venue,
                                "created_at": _now_minute(),
                                "updated_at": _now_minute(),
                            }
                        )
                    save_db(db)
                if participant_ids:
                    return redirect(url_for("add_student", name=unknowns[0], next=url_for("record_lesson")))
                # Only new students: keep page minimal and force re-input / add student.
                return render_template(
                    "add_lesson.html",
                    error=(
                        (f"检测到新学员：{', '.join(unknowns)}。请先新增学员，再回到这里进行智能录入。")
                        if get_lang() == "zh"
                        else (f"New student(s) detected: {', '.join(unknowns)}. Please add them first, then come back to Smart input.")
                    ),
                    students=students,
                    student_rows=student_rows,
                    form={"lesson_text": text, "class_type": "", "student_id": ""},
                    new_student={"name": unknowns[0], "unknowns": ", ".join(unknowns)},
                    venues=_venues(db),
                    lesson_type_prices=lesson_type_prices,
                )

            # duration_minutes is locked by class type baseline (1:1 => 60min; others => 120min)
            duration_minutes = None

            # topics
            training = str(parsed.get("training") or "")
            topics = [t.strip() for t in re.split(r"[、,，/]+", training) if t.strip()] if training else []

            coach_id = (
                str((db.get("coaches") or [{}])[0].get("id") or "") if isinstance(db.get("coaches"), list) else ""
            )
            # Split multi-student input into separate lessons (one per student)
            if len(participant_ids) >= 2:
                saved_ids = []
                for sid in participant_ids:
                    s_obj = next((x for x in db.get("students", []) if isinstance(x, dict) and str(x.get("id")) == str(sid)), None)
                    s_name = str((s_obj or {}).get("name") or "")
                    default_code = str((s_obj or {}).get("default_class_type") or "").strip()
                    chosen_code = class_type_override or default_code or ""
                    lesson_type_id = _lesson_type_id_by_code(db, chosen_code) if chosen_code else None
                    snapshot_ppl = 0.0
                    snapshot_currency = "SGD"
                    for lt in db.get("lesson_types", []):
                        if isinstance(lt, dict) and str(lt.get("id") or "") == str(lesson_type_id or ""):
                            try:
                                snapshot_ppl = float(lt.get("price_per_lesson") or 0.0)
                            except Exception:
                                snapshot_ppl = 0.0
                            snapshot_currency = str(lt.get("currency") or snapshot_currency)
                            break
                    venue = venue_override or str(parsed.get("venue") or "") or str((s_obj or {}).get("venue") or "")
                    duration_minutes = _baseline_minutes_for_class_type(chosen_code) if chosen_code else None
                    is_draft = False
                    if not s_name or duration_minutes is None or not chosen_code or not venue or lesson_type_id is None:
                        is_draft = True
                    lid = _new_id()
                    db["lessons"].append(
                        {
                            "id": lid,
                            "coach_id": coach_id,
                            "lesson_type_id": lesson_type_id,
                            "class_type": chosen_code,
                            "price_per_lesson": snapshot_ppl,
                            "baseline_minutes": _baseline_minutes_for_class_type(chosen_code),
                            "price_per_hour": (
                                snapshot_ppl / (_baseline_minutes_for_class_type(chosen_code) / 60.0)
                            )
                            if snapshot_ppl
                            else 0.0,
                            "currency": snapshot_currency,
                            "start_at": str(parsed.get("date") or _now_minute()),
                            "duration_minutes": duration_minutes,
                            "participant_ids": [str(sid)],
                            "participant_names_raw": [],
                            "draft": is_draft,
                            "topics": topics,
                            "status": str(parsed.get("status") or "未说明"),
                            "next_plan_time": parsed.get("next_plan_time") or None,
                            "next_plan": "未说明" if parsed.get("next_plan") is None else str(parsed.get("next_plan") or ""),
                            "original_text": str(parsed.get("original_text") or ""),
                            "venue": venue,
                            "created_at": _now_minute(),
                            "updated_at": _now_minute(),
                        }
                    )
                    saved_ids.append(lid)
                save_db(db)
                return redirect(url_for("edit_lesson", lesson_id=str(saved_ids[0])))

            # Single-student path
            default_code = ""
            if participant_ids and student_name:
                s0 = _student_by_name(db, student_name)
                if isinstance(s0, dict) and str(s0.get("default_class_type") or "").strip():
                    default_code = _normalize_class_type_code(str(s0.get("default_class_type") or "")) or ""

            # Hard rule: if we didn't resolve to an existing student, do NOT auto-fill class type or venue.
            # This prevents accidental misclassification when name detection fails.
            if not participant_ids:
                chosen_code = ""
                lesson_type_id = None
            else:
                chosen_code = class_type_override or default_code or ""
                lesson_type_id = _lesson_type_id_by_code(db, chosen_code) if chosen_code else None

            # Snapshot pricing at time of lesson creation (locks history)
            snapshot_ppl = 0.0
            snapshot_currency = "SGD"
            for lt in db.get("lesson_types", []):
                if isinstance(lt, dict) and str(lt.get("id") or "") == str(lesson_type_id or ""):
                    try:
                        snapshot_ppl = float(lt.get("price_per_lesson") or 0.0)
                    except Exception:
                        snapshot_ppl = 0.0
                    snapshot_currency = str(lt.get("currency") or snapshot_currency)
                    break

            venue = ""
            if participant_ids:
                venue = venue_override or str(parsed.get("venue") or "")
                if not venue and student_name:
                    s0 = _student_by_name(db, student_name)
                    if isinstance(s0, dict):
                        venue = str(s0.get("venue") or "")

            duration_minutes = _baseline_minutes_for_class_type(chosen_code) if chosen_code else None

            is_draft = False
            if not student_name or duration_minutes is None or not chosen_code or lesson_type_id is None:
                is_draft = True
            if unknowns:
                is_draft = True

            lesson = {
                "id": _new_id(),
                "coach_id": coach_id,
                "lesson_type_id": lesson_type_id,
                "class_type": chosen_code,
                "price_per_lesson": snapshot_ppl,
                # keep derived hourly for backward compatibility
                "baseline_minutes": _baseline_minutes_for_class_type(chosen_code),
                "price_per_hour": (
                    snapshot_ppl / (_baseline_minutes_for_class_type(chosen_code) / 60.0)
                )
                if snapshot_ppl
                else 0.0,
                "currency": snapshot_currency,
                "start_at": str(parsed.get("date") or _now_minute()),
                "duration_minutes": duration_minutes,
                "participant_ids": participant_ids,
                "participant_names_raw": (
                    []
                    if participant_ids and not unknowns
                    else (unknowns if unknowns else ([student_name] if student_name else []))
                ),
                "draft": is_draft,
                "topics": topics,
                "status": str(parsed.get("status") or "未说明"),
                "next_plan_time": parsed.get("next_plan_time") or None,
                # Preserve explicit empty string (means "no plan content provided").
                # Only default to "未说明" when the field is missing (None).
                "next_plan": "未说明" if parsed.get("next_plan") is None else str(parsed.get("next_plan") or ""),
                "original_text": str(parsed.get("original_text") or ""),
                "venue": venue,
                "created_at": _now_minute(),
                "updated_at": _now_minute(),
            }

            db["lessons"].append(lesson)
            save_db(db)
            # After saving, go to edit page so coach can verify/adjust parsing.
            return redirect(url_for("edit_lesson", lesson_id=str(lesson.get("id") or "")))
        except Exception as e:
            msg = str(e) or "Unknown error"
            if get_lang() == "zh":
                msg = f"保存失败：{msg}"
            else:
                msg = f"Save failed: {msg}"
            return render_template(
                "add_lesson.html",
                error=msg,
                students=students,
                student_rows=student_rows,
                form={
                    "lesson_text": (request.form.get("lesson_text") or "").strip(),
                    "class_type": (request.form.get("class_type") or "").strip(),
                    "student_id": (request.form.get("student_id") or "").strip(),
                },
                venues=_venues(db),
                lesson_type_prices=lesson_type_prices,
            )

    return render_template(
        "add_lesson.html",
        error=None,
        students=students,
        student_rows=student_rows,
        form={"lesson_text": pending_text or "", "class_type": pending_ct or "", "student_id": pending_sid or ""},
        venues=_venues(db),
        lesson_type_prices=lesson_type_prices,
    )


@app.route("/lessons/<lesson_id>/edit", methods=["GET", "POST"])
def edit_lesson(lesson_id: str):
    db = load_db()
    lesson = get_lesson_or_404(db, lesson_id)

    student_rows = [
        {
            "name": str(s.get("name") or ""),
            "default_class_type": _normalize_class_type_code(str(s.get("default_class_type") or "1:1")) or "1:1",
            "venue": str(s.get("venue") or ""),
        }
        for s in db.get("students", [])
        if isinstance(s, dict) and s.get("name")
    ]
    student_rows.sort(key=lambda x: x["name"])
    student_defaults = {
        r["name"]: {"default_class_type": r["default_class_type"], "venue": r["venue"]} for r in student_rows if r["name"]
    }
    lesson_type_prices = {}
    for lt in db.get("lesson_types", []):
        if not isinstance(lt, dict):
            continue
        code = str(lt.get("code") or "").strip()
        if code not in ("1:1", "1:2", "1:3", "1:4"):
            continue
        try:
            ppl = float(lt.get("price_per_lesson") or 0.0)
        except Exception:
            ppl = 0.0
        lesson_type_prices[code] = {"price_per_lesson": ppl, "currency": str(lt.get("currency") or "SGD")}

    if request.method == "POST":
        class_type = (request.form.get("class_type") or "").strip()
        venue = (request.form.get("venue") or "").strip()
        student = (request.form.get("student") or "").strip()
        training = (request.form.get("training") or "").strip() or "未说明"
        status = (request.form.get("status") or "").strip() or "未说明"
        next_plan_time = (request.form.get("next_plan_time") or "").strip()
        # allow blank next_plan
        next_plan = (request.form.get("next_plan") or "").strip()
        original_text = (request.form.get("original_text") or "").strip()
        date = (request.form.get("date") or "").strip() or _now_minute()

        # Student is required to save changes
        if not student:
            students = sorted(
                [str(s.get("name")) for s in db.get("students", []) if isinstance(s, dict) and s.get("name")]
            )
            venues = _venues(db)
            view = _lesson_view(db, lesson)
            view["id"] = lesson.get("id")
            view["student"] = ""
            # keep any entered fields for convenience
            # if student is missing, do not prefill other required fields
            view["class_type"] = ""
            view["venue"] = ""
            view["duration"] = ""
            view["training"] = training
            view["status"] = status
            view["next_plan_time"] = next_plan_time
            view["next_plan"] = next_plan
            view["original_text"] = original_text
            view["date"] = date
            # fee fields for template safety
            try:
                view["total_fee"] = float(_lesson_fee(db, lesson))
            except Exception:
                view["total_fee"] = None
            view["currency"] = str(lesson.get("currency") or "SGD")
            return render_template(
                "lesson_edit.html",
                lesson=view,
                students=students,
                venues=venues,
                student_defaults=student_defaults,
                lesson_type_prices=lesson_type_prices,
                can_delete=False,
                error=(
                    "必要字段请输入：学员姓名"
                    if get_lang() == "zh"
                    else "Required field missing: student name"
                ),
            )

        # Student must exist in DB (no inline creation here)
        s_exist = _student_by_name(db, student)
        if not s_exist:
            students = sorted(
                [str(s.get("name")) for s in db.get("students", []) if isinstance(s, dict) and s.get("name")]
            )
            venues = _venues(db)
            view = _lesson_view(db, lesson)
            view["id"] = lesson.get("id")
            view["student"] = student
            view["class_type"] = class_type
            view["venue"] = venue
            view["duration"] = _lesson_view(db, lesson).get("duration")
            view["training"] = training
            view["status"] = status
            view["next_plan_time"] = next_plan_time
            view["next_plan"] = next_plan
            view["original_text"] = original_text
            view["date"] = date
            try:
                view["total_fee"] = float(_lesson_fee(db, lesson))
            except Exception:
                view["total_fee"] = None
            view["currency"] = str(lesson.get("currency") or "SGD")
            return render_template(
                "lesson_edit.html",
                lesson=view,
                students=students,
                venues=venues,
                student_defaults=student_defaults,
                lesson_type_prices=lesson_type_prices,
                can_delete=False,
                error=("学员不存在，请先到“学员”里新增。" if get_lang() == "zh" else "Student not found. Please add the student first."),
            )

        # Class type is required; if blank, try load from student's default first
        if not class_type:
            default_ct = _normalize_class_type_code(str(s_exist.get("default_class_type") or "")) if isinstance(s_exist, dict) else ""
            class_type = default_ct or ""
        if class_type not in ("1:1", "1:2", "1:3", "1:4"):
            students = sorted(
                [str(s.get("name")) for s in db.get("students", []) if isinstance(s, dict) and s.get("name")]
            )
            venues = _venues(db)
            view = _lesson_view(db, lesson)
            view["id"] = lesson.get("id")
            view["student"] = student
            view["class_type"] = ""
            view["venue"] = venue
            view["duration"] = "" if duration in ("未说明",) else duration
            view["training"] = training
            view["status"] = status
            view["next_plan_time"] = next_plan_time
            view["next_plan"] = next_plan
            view["original_text"] = original_text
            view["date"] = date
            try:
                view["total_fee"] = float(_lesson_fee(db, lesson))
            except Exception:
                view["total_fee"] = None
            view["currency"] = str(lesson.get("currency") or "SGD")
            return render_template(
                "lesson_edit.html",
                lesson=view,
                students=students,
                venues=venues,
                student_defaults=student_defaults,
                lesson_type_prices=lesson_type_prices,
                can_delete=False,
                error=("必要字段请输入：课程模式" if get_lang() == "zh" else "Required field missing: class type"),
            )

        # Venue is required
        if not venue:
            students = sorted(
                [str(s.get("name")) for s in db.get("students", []) if isinstance(s, dict) and s.get("name")]
            )
            venues = _venues(db)
            view = _lesson_view(db, lesson)
            view["id"] = lesson.get("id")
            view["student"] = student
            view["class_type"] = class_type
            view["venue"] = ""
            view["duration"] = "" if duration in ("未说明",) else duration
            view["training"] = training
            view["status"] = status
            view["next_plan_time"] = next_plan_time
            view["next_plan"] = next_plan
            view["original_text"] = original_text
            view["date"] = date
            try:
                view["total_fee"] = float(_lesson_fee(db, lesson))
            except Exception:
                view["total_fee"] = None
            view["currency"] = str(lesson.get("currency") or "SGD")
            return render_template(
                "lesson_edit.html",
                lesson=view,
                students=students,
                venues=venues,
                student_defaults=student_defaults,
                lesson_type_prices=lesson_type_prices,
                can_delete=False,
                error=("必要字段请输入：场地" if get_lang() == "zh" else "Required field missing: venue"),
            )

        # Update lesson_type_id
        lt_id = _lesson_type_id_by_code(db, class_type)
        if lt_id:
            lesson["lesson_type_id"] = lt_id
            # Refresh snapshot to match selected class type (explicit manual edit)
            for lt in db.get("lesson_types", []):
                if isinstance(lt, dict) and str(lt.get("id") or "") == str(lt_id):
                    try:
                        lesson["price_per_lesson"] = float(lt.get("price_per_lesson") or 0.0)
                    except Exception:
                        lesson["price_per_lesson"] = 0.0
                    lesson["currency"] = str(lt.get("currency") or "SGD")
                    lesson["class_type"] = class_type
                    lesson["baseline_minutes"] = _baseline_minutes_for_class_type(class_type)
                    # keep derived hourly for backward compatibility
                    try:
                        lesson["price_per_hour"] = float(lesson.get("price_per_lesson") or 0.0) / (
                            _baseline_minutes_for_class_type(class_type) / 60.0
                        )
                    except Exception:
                        lesson["price_per_hour"] = lesson.get("price_per_hour") or 0.0
                    break

        # Update participant
        participant_ids: list[str] = []
        if student:
            s = _student_by_name(db, student)
            participant_ids = [str(s.get("id"))] if isinstance(s, dict) and s.get("id") else []

        lesson["participant_ids"] = participant_ids
        lesson["participant_names_raw"] = [] if participant_ids else ([student] if student else [])

        # duration_minutes is locked by class type baseline
        lesson["duration_minutes"] = _baseline_minutes_for_class_type(class_type) if class_type else None

        # topics
        lesson["topics"] = [t.strip() for t in re.split(r"[、,，/]+", training) if t.strip()] if training else []

        lesson["status"] = status
        lesson["next_plan"] = next_plan
        lesson["original_text"] = original_text
        lesson["start_at"] = date
        lesson["venue"] = venue
        lesson["updated_at"] = _now_minute()
        lesson["draft"] = False

        if next_plan_time:
            lesson["next_plan_time"] = next_plan_time
        else:
            lesson.pop("next_plan_time", None)

        save_db(db)
        if student != "未知":
            return redirect(url_for("student_progress", name=student))
        return redirect(url_for("index"))

    students = sorted([str(s.get("name")) for s in db.get("students", []) if isinstance(s, dict) and s.get("name")])
    venues = _venues(db)
    # Provide a compatibility view to the template
    view = _lesson_view(db, lesson)
    # Include raw ids for edit action
    view["id"] = lesson.get("id")
    # Pricing snapshot display
    try:
        view["price_per_hour"] = float(lesson.get("price_per_hour")) if lesson.get("price_per_hour") is not None else None
    except Exception:
        view["price_per_hour"] = None
    view["currency"] = str(lesson.get("currency") or "")
    try:
        amount_pp, cur = _lesson_per_person_fee(db, lesson)
        view["per_person_fee"] = float(amount_pp)
        view["currency"] = view["currency"] or str(cur or "")
    except Exception:
        view["per_person_fee"] = None
    try:
        view["total_fee"] = float(_lesson_fee(db, lesson))
    except Exception:
        view["total_fee"] = None
    pids = lesson.get("participant_ids") or []
    view["participants"] = len(pids) if isinstance(pids, list) else 0
    # hide fee for drafts / incomplete fields
    if bool(lesson.get("draft")) or not lesson.get("duration_minutes") or not lesson.get("price_per_lesson"):
        view["total_fee"] = None
    # Delete policy: only allow deleting lessons in the most recent 30 days
    can_delete = False
    try:
        dt = _parse_dt(lesson.get("start_at"))
        if dt and (datetime.now().date() - dt.date()).days <= 30:
            can_delete = True
    except Exception:
        can_delete = False

    return render_template(
        "lesson_edit.html",
        lesson=view,
        students=students,
        venues=venues,
        student_defaults=student_defaults,
        lesson_type_prices=lesson_type_prices,
        can_delete=can_delete,
        error=None,
    )


@app.post("/lessons/<lesson_id>/delete")
def delete_lesson(lesson_id: str):
    """
    Delete a lesson record and related payment rows.
    Finance/statistics will automatically reflect the deletion.
    """
    db = load_db()
    lesson = get_lesson_or_404(db, lesson_id)
    lid = str(lesson.get("id") or "")

    # Policy: do not allow deleting lessons older than the most recent 30 days
    try:
        dt = _parse_dt(lesson.get("start_at"))
    except Exception:
        dt = None
    if dt and (datetime.now().date() - dt.date()).days > 30:
        msg = (
            "为保证数据一致性与统计准确性，系统不支持删除超过最近一个月的课程记录。请联系管理员。"
            if get_lang() == "zh"
            else "To keep data consistent and reports accurate, deleting lessons older than the most recent 30 days is disabled. Please contact the administrator."
        )
        abort(403, description=msg)

    lessons = db.get("lessons", [])
    if isinstance(lessons, list):
        before = len(lessons)
        db["lessons"] = [l for l in lessons if not (isinstance(l, dict) and str(l.get("id") or "") == lid)]
        if len(db["lessons"]) != before:
            pass

    payments = db.get("payments", [])
    if isinstance(payments, list) and lid:
        before_p = len(payments)
        payments = [p for p in payments if not (isinstance(p, dict) and str(p.get("lesson_id") or "") == lid)]
        payments = [
            p
            for p in payments
            if not (
                isinstance(p, dict)
                and isinstance(p.get("lesson_ids"), list)
                and lid in [str(x) for x in p.get("lesson_ids")]
            )
        ]
        if len(payments) != before_p:
            db["payments"] = payments

    save_db(db)
    next_url = (request.form.get("next") or "").strip()
    return redirect(next_url or request.referrer or url_for("index"))


@app.post("/lessons/<lesson_id>/mark_paid")
def mark_lesson_paid(lesson_id: str):
    db = load_db()
    lesson = get_lesson_or_404(db, lesson_id)
    pids = lesson.get("participant_ids") or []
    if not isinstance(pids, list) or not pids:
        return redirect(url_for("index"))

    amount_pp, currency = _lesson_per_person_fee(db, lesson)
    payments = db.get("payments", [])
    if not isinstance(payments, list):
        payments = []
        db["payments"] = payments

    lid = str(lesson.get("id"))
    for sid in pids:
        sid = str(sid)
        if any(isinstance(p, dict) and _payment_matches_lesson(p, lid, sid) for p in payments):
            continue
        payments.append(
            {
                "id": _new_id(),
                "student_id": sid,
                "lesson_id": lid,
                "amount": float(amount_pp),
                "currency": currency,
                "paid_at": _now_minute(),
                "method": "manual",
                "note": "Marked paid from lesson list.",
                "lesson_ids": [lid],
            }
        )

    save_db(db)
    return redirect(request.referrer or url_for("index"))


@app.post("/lessons/<lesson_id>/mark_unpaid")
def mark_lesson_unpaid(lesson_id: str):
    db = load_db()
    lesson = get_lesson_or_404(db, lesson_id)
    pids = lesson.get("participant_ids") or []
    if not isinstance(pids, list) or not pids:
        return redirect(url_for("index"))

    payments = db.get("payments", [])
    if not isinstance(payments, list):
        return redirect(request.referrer or url_for("index"))

    lid = str(lesson.get("id"))
    before = len(payments)
    payments = [p for p in payments if not (isinstance(p, dict) and str(p.get("lesson_id") or "") == lid)]
    # also drop legacy lesson_ids references
    payments = [p for p in payments if not (isinstance(p, dict) and isinstance(p.get("lesson_ids"), list) and lid in [str(x) for x in p.get("lesson_ids")])]
    if len(payments) != before:
        db["payments"] = payments
        save_db(db)
    return redirect(request.referrer or url_for("index"))


@app.post("/payments/mark_paid")
def mark_paid_for_student_lesson():
    """
    Mark paid for a single (student_id, lesson_id) pair.
    Used by finance "unpaid" list so it doesn't force paying all participants.
    """
    db = load_db()
    payload = request.get_json(silent=True) or {}
    lesson_id = str(payload.get("lesson_id") or "").strip()
    student_id = str(payload.get("student_id") or "").strip()
    if not lesson_id or not student_id:
        return {"ok": False, "error": "missing fields"}, 400

    lesson = get_lesson_or_404(db, lesson_id)
    pids = lesson.get("participant_ids") or []
    if not (isinstance(pids, list) and student_id in [str(x) for x in pids]):
        return {"ok": False, "error": "student not in lesson"}, 400

    payments = db.get("payments", [])
    if not isinstance(payments, list):
        payments = []
        db["payments"] = payments

    if any(isinstance(p, dict) and _payment_matches_lesson(p, lesson_id, student_id) for p in payments):
        return {"ok": True, "already": True}

    amount_pp, currency = _lesson_per_person_fee(db, lesson)
    payments.append(
        {
            "id": _new_id(),
            "student_id": student_id,
            "lesson_id": lesson_id,
            "amount": float(amount_pp),
            "currency": currency,
            "paid_at": _now_minute(),
            "method": "manual",
            "note": "Marked paid from finance unpaid list.",
            "lesson_ids": [lesson_id],
        }
    )
    save_db(db)
    return {"ok": True, "amount": float(amount_pp), "currency": currency}


@app.get("/students/<name>")
def student_progress(name: str):
    db = load_db()
    student = get_student_or_404(db, name)
    sid = str(student.get("id") or "")
    lessons_all_raw = [
        l
        for l in db.get("lessons", [])
        if isinstance(l, dict) and sid and sid in (l.get("participant_ids") or [])
    ]
    lessons_all_raw.sort(key=_lesson_start_key, reverse=True)
    lessons_all = [_lesson_view(db, l) for l in lessons_all_raw]

    summary = {"total_lessons": len(lessons_all), "last_lesson_date": lessons_all[0]["date"] if lessons_all else None}
    return render_template(
        "student_progress.html",
        name=name,
        student=student,
        lessons=lessons_all,
        summary=summary,
    )


@app.post("/students/<name>/payment")
def update_payment(name: str):
    db = load_db()
    student = get_student_or_404(db, name)
    paid = (request.form.get("paid") or "no").lower() == "yes"
    sid = str(student.get("id") or "")
    if not sid:
        abort(400, description="Invalid student")

    payments = db.get("payments", [])
    if not isinstance(payments, list):
        payments = []
        db["payments"] = payments

    if paid:
        if not _student_paid(db, sid):
            payments.append(
                {
                    "id": _new_id(),
                    "student_id": sid,
                    "amount": 0.0,
                    "currency": "CNY",
                    "paid_at": _now_minute(),
                    "method": "manual",
                    "note": "Marked paid (no amount).",
                    "lesson_ids": [],
                }
            )
    else:
        db["payments"] = [p for p in payments if not (isinstance(p, dict) and p.get("student_id") == sid)]

    save_db(db)
    return redirect(url_for("payments"))


@app.get("/payments")
def payments():
    db = load_db()
    students_rows = []
    for s in db.get("students", []):
        if isinstance(s, dict) and s.get("id") and s.get("name"):
            students_rows.append(
                (
                    str(s["name"]),
                    {
                        "phone": s.get("contact") or "",
                        "paid": _student_paid(db, str(s["id"])),
                        "created_at": s.get("created_at") or "",
                    },
                )
            )
    students_rows.sort(key=lambda x: x[0])
    paid_students = [(n, s) for n, s in students_rows if s.get("paid")]
    unpaid_students = [(n, s) for n, s in students_rows if not s.get("paid")]
    return render_template("payments.html", paid_students=paid_students, unpaid_students=unpaid_students)


if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", "5000"))
    app.run(debug=True, port=port)

