import { NextResponse } from "next/server";
import { createLessonForStudent, extractKnownStudentNames, findStudentById, findStudentByName, readDb, writeDb } from "@/lib/db";
import { supabaseAdmin } from "@/lib/supabaseAdmin";
import type { Student, LessonType, Coach } from "@/lib/types";

export async function POST(req: Request) {
  const body = await req.json().catch(() => ({}));
  const text = String(body?.text || "").trim();
  const studentId = String(body?.studentId || "").trim();
  const classType = String(body?.classType || "").trim();
  const venue = String(body?.venue || "").trim();

  if (!text) {
    return NextResponse.json({ ok: false, error: "lesson_text_required" }, { status: 400 });
  }

  const supa = supabaseAdmin();
  if (supa) {
    // Resolve student
    let student: Student | null = null;
    if (studentId) {
      const { data } = await supa.from("students").select("*").eq("id", studentId).maybeSingle();
      student = (data as Student | null) || null;
    }
    if (!student) {
      const { data: students } = await supa.from("students").select("*");
      const names = extractKnownStudentNames(text, (students || []) as Student[]);
      if (names.length === 1) {
        const { data } = await supa.from("students").select("*").eq("name", names[0]).maybeSingle();
        student = (data as Student | null) || null;
      }
    }

    if (!student) {
      return NextResponse.json({ ok: false, error: "student_not_resolved" }, { status: 422 });
    }

    // Read lesson_types and coaches for snapshot
    const [{ data: lessonTypes }, { data: coaches }] = await Promise.all([
      supa.from("lesson_types").select("*"),
      supa.from("coaches").select("*").limit(1),
    ]);
    const dbLike = {
      schema_version: 1,
      students: [student],
      lessons: [],
      lesson_types: (lessonTypes || []) as LessonType[],
      payments: [],
      coaches: (coaches || []) as Coach[],
    };

    const lesson = createLessonForStudent({
      db: dbLike,
      student,
      originalText: text,
      classTypeOverride: classType,
      venueOverride: venue,
    });

    const { error } = await supa.from("lessons").insert({
      ...lesson,
      participant_ids: lesson.participant_ids || [],
      participant_names_raw: lesson.participant_names_raw || [],
      topics: lesson.topics || [],
    });
    if (error) {
      return NextResponse.json({ ok: false, error: "db_insert_failed", detail: error.message }, { status: 500 });
    }
    return NextResponse.json({ ok: true, lessonId: lesson.id });
  }

  const db = await readDb();

  let student = studentId ? findStudentById(db, studentId) : undefined;
  if (!student) {
    // fallback: match by known names appearing in text
    const names = extractKnownStudentNames(text, db.students);
    if (names.length === 1) {
      student = findStudentByName(db, names[0]);
    }
  }

  if (!student) {
    return NextResponse.json({ ok: false, error: "student_not_resolved" }, { status: 422 });
  }

  const lesson = createLessonForStudent({
    db,
    student,
    originalText: text,
    classTypeOverride: classType,
    venueOverride: venue,
  });

  db.lessons.push(lesson);
  await writeDb(db);

  return NextResponse.json({ ok: true, lessonId: lesson.id });
}

