import { promises as fs } from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import type { CoachDb, ClassTypeCode, Lesson, LessonType, Payment, Student, Coach } from "@/lib/types";

const WORKSPACE_ROOT = path.resolve(process.cwd(), "..");
const DB_PATH = path.join(WORKSPACE_ROOT, "coach_data.json");

function nowMinute() {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd} ${hh}:${mi}`;
}

export function normalizeClassType(code: string): ClassTypeCode | "" {
  const s = String(code || "")
    .trim()
    .replaceAll("：", ":")
    .replaceAll(" ", "");
  if (s === "1:1" || s === "1:2" || s === "1:3" || s === "1:4") return s;
  return "";
}

export function baselineMinutesForClassType(code: string): 60 | 120 {
  return normalizeClassType(code) === "1:1" ? 60 : 120;
}

function newId() {
  return crypto.randomBytes(16).toString("hex");
}

function ensureShape(db: unknown): CoachDb {
  const src = (db && typeof db === "object") ? (db as Record<string, unknown>) : {};
  const out: CoachDb = {
    schema_version: typeof src.schema_version === "number" ? src.schema_version : 1,
    students: Array.isArray(src.students) ? (src.students as Student[]) : [],
    lessons: Array.isArray(src.lessons) ? (src.lessons as Lesson[]) : [],
    lesson_types: Array.isArray(src.lesson_types) ? (src.lesson_types as LessonType[]) : [],
    payments: Array.isArray(src.payments) ? (src.payments as Payment[]) : [],
    coaches: Array.isArray(src.coaches) ? (src.coaches as Coach[]) : [],
    finance_reports: src.finance_reports,
  };
  return out;
}

export async function readDb(): Promise<CoachDb> {
  let raw: string;
  try {
    raw = await fs.readFile(DB_PATH, "utf-8");
  } catch (e: any) {
    if (e && (e.code === "ENOENT" || e.code === "ENOTDIR")) {
      // In serverless environments (e.g., Vercel) this file won't exist.
      return ensureShape({ schema_version: 1 });
    }
    throw e;
  }
  const parsed = JSON.parse(raw);
  const db = ensureShape(parsed);
  // light normalization
  for (const s of db.students) {
    if (!s || typeof s !== "object") continue;
    s.default_class_type = normalizeClassType(String(s.default_class_type || "")) || "1:1";
    s.venue = String(s.venue || "");
  }
  for (const l of db.lessons) {
    if (!l || typeof l !== "object") continue;
    if (l.class_type != null) l.class_type = normalizeClassType(String(l.class_type || ""));
  }
  return db;
}

export async function writeDb(db: CoachDb): Promise<void> {
  const tmp = `${DB_PATH}.tmp`;
  await fs.writeFile(tmp, JSON.stringify(db, null, 2) + "\n", "utf-8");
  await fs.rename(tmp, DB_PATH);
}

export function findStudentById(db: CoachDb, id: string): Student | undefined {
  return db.students.find((s) => s && typeof s === "object" && String(s.id) === String(id));
}

export function findStudentByName(db: CoachDb, name: string): Student | undefined {
  const n = String(name || "").trim();
  if (!n) return undefined;
  return db.students.find((s) => s && typeof s === "object" && String(s.name) === n);
}

export function extractKnownStudentNames(text: string, students: Student[]): string[] {
  const t = String(text || "");
  const low = t.toLowerCase();
  const hits: { idx: number; name: string }[] = [];
  for (const s of students) {
    const name = String(s?.name || "").trim();
    if (!name) continue;
    const i = low.indexOf(name.toLowerCase());
    if (i >= 0) hits.push({ idx: i, name });
  }
  hits.sort((a, b) => a.idx - b.idx || b.name.length - a.name.length);
  const out: string[] = [];
  const seen = new Set<string>();
  for (const h of hits) {
    if (seen.has(h.name)) continue;
    seen.add(h.name);
    out.push(h.name);
  }
  return out;
}

export function todayKey() {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

export function createLessonForStudent(args: {
  db: CoachDb;
  student: Student;
  originalText: string;
  classTypeOverride?: string;
  venueOverride?: string;
}): Lesson {
  const { db, student, originalText } = args;
  const classType = normalizeClassType(args.classTypeOverride || student.default_class_type || "1:1") || "1:1";
  const baselineMinutes = baselineMinutesForClassType(classType);
  const venue = String(args.venueOverride || student.venue || "");

  // price snapshot from current lesson_types
  let pricePerLesson = 0;
  let currency = "SGD";
  const lt = db.lesson_types.find((x) => x && typeof x === "object" && String(x.code) === classType);
  if (lt) {
    pricePerLesson = Number(lt.price_per_lesson || 0);
    currency = String(lt.currency || currency);
  }

  const coachId = String(db.coaches?.[0]?.id || "");
  const lessonTypeId = lt ? String(lt.id || "") : null;

  return {
    id: newId(),
    coach_id: coachId,
    lesson_type_id: lessonTypeId,
    class_type: classType,
    baseline_minutes: baselineMinutes,
    price_per_lesson: pricePerLesson,
    currency,
    start_at: nowMinute(),
    duration_minutes: baselineMinutes,
    participant_ids: [String(student.id)],
    participant_names_raw: [],
    draft: !venue || !classType,
    topics: [],
    status: "未说明",
    next_plan_time: null,
    next_plan: "",
    original_text: String(originalText || ""),
    venue,
    created_at: nowMinute(),
    updated_at: nowMinute(),
  };
}

