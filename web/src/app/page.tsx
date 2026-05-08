import Link from "next/link";
import { readDb, todayKey } from "@/lib/db";
import { supabaseAdmin } from "@/lib/supabaseAdmin";
import type { Lesson, Student } from "@/lib/types";
import { SmartLessonForm } from "@/app/components/SmartLessonForm";

export const dynamic = "force-dynamic";

export default async function Home() {
  const today = todayKey();
  const supa = supabaseAdmin();

  let todays: Lesson[] = [];
  let studentById = new Map<string, Student>();
  let students: Student[] = [];
  let venues: string[] = [];
  const lessonTypePrices: Record<string, { price_per_lesson: number; currency: string }> = {};

  if (supa) {
    const [{ data: lessons }, { data: studentsData }, { data: lessonTypes }, { data: coaches }] = await Promise.all([
      supa.from("lessons").select("*").like("start_at", `${today}%`),
      supa.from("students").select("*"),
      supa.from("lesson_types").select("*"),
      supa.from("coaches").select("*").limit(1),
    ]);
    todays = (lessons || []) as Lesson[];
    todays.sort((a, b) => String(a.start_at || "").localeCompare(String(b.start_at || "")));
    students = (studentsData || []) as Student[];
    studentById = new Map(students.map((s) => [String(s.id), s]));
    const coach0: any = Array.isArray(coaches) && coaches.length ? coaches[0] : null;
    const v = Array.isArray(coach0?.venues) ? coach0.venues : [];
    venues = v.map((x: any) => String(x || "")).filter((x: string) => x.trim());
    for (const lt of (lessonTypes || []) as any[]) {
      const code = String(lt?.code || "");
      if (!code) continue;
      lessonTypePrices[code] = {
        price_per_lesson: Number(lt?.price_per_lesson || 0),
        currency: String(lt?.currency || "SGD"),
      };
    }
  } else {
    const db = await readDb();
    todays = db.lessons
      .filter((l) => String(l?.start_at || "").startsWith(today))
      .slice()
      .sort((a, b) => String(a.start_at || "").localeCompare(String(b.start_at || "")));
    studentById = new Map(db.students.map((s) => [String(s.id), s]));
    students = db.students as any;
    venues = (db.coaches?.[0]?.venues as any) || [];
    for (const lt of (db.lesson_types as any[]) || []) {
      const code = String(lt?.code || "");
      if (!code) continue;
      lessonTypePrices[code] = {
        price_per_lesson: Number(lt?.price_per_lesson || 0),
        currency: String(lt?.currency || "SGD"),
      };
    }
  }
  return (
    <main style={{ maxWidth: 980, margin: "0 auto", padding: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <h1 style={{ fontSize: 18, fontWeight: 700 }}>Coach Agent (Next.js)</h1>
        <div style={{ display: "flex", gap: 12 }}>
          <Link href="/">Home</Link>
        </div>
      </div>

      <section style={{ marginTop: 16 }}>
        <SmartLessonForm
          students={students.map((s) => ({ id: String(s.id), name: String(s.name), default_class_type: String((s as any).default_class_type || ""), venue: String((s as any).venue || "") }))}
          venues={venues}
          lessonTypePrices={lessonTypePrices}
          onCreated={undefined}
        />
      </section>

      <section style={{ marginTop: 16 }}>
        <h2 style={{ fontSize: 14, opacity: 0.8 }}>Today’s lessons</h2>
        {todays.length === 0 ? (
          <div style={{ opacity: 0.65, marginTop: 8 }}>No lessons yet.</div>
        ) : (
          <div style={{ marginTop: 8, display: "grid", gap: 8 }}>
            {todays.map((l) => {
              const sid = Array.isArray(l.participant_ids) ? String(l.participant_ids[0] || "") : "";
              const s = studentById.get(sid);
              return (
                <div
                  key={l.id}
                  style={{
                    border: "1px solid rgba(255,255,255,.10)",
                    borderRadius: 12,
                    padding: 12,
                    background: "rgba(255,255,255,.04)",
                  }}
                >
                  <div style={{ opacity: 0.7, fontSize: 12 }}>{String(l.start_at || "").slice(0, 16)}</div>
                  <div style={{ marginTop: 6 }}>
                    <strong>{s?.name || "—"}</strong>{" "}
                    <span style={{ opacity: 0.7 }}>
                      {l.class_type || "—"} · {l.venue || "—"}
                    </span>
                  </div>
                  <div style={{ opacity: 0.7, marginTop: 6, fontSize: 12 }}>{l.original_text || ""}</div>
                </div>
              );
            })}
          </div>
        )}
      </section>
    </main>
  );
}
