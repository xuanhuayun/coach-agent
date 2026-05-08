import { supabaseAdmin } from "@/lib/supabaseAdmin";
import { readDb } from "@/lib/db";
import type { Student } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function StudentsPage() {
  const supa = supabaseAdmin();
  let students: Student[] = [];
  if (supa) {
    const { data } = await supa.from("students").select("*");
    students = (data || []) as Student[];
  } else {
    const db = await readDb();
    students = db.students as any;
  }
  students.sort((a, b) => String(a.name || "").localeCompare(String(b.name || "")));

  return (
    <main>
      <div className="card">
        <div className="row">
          <h2 style={{ margin: 0, fontSize: 16 }}>Students</h2>
        </div>
        <div style={{ marginTop: 12, display: "grid", gap: 10 }}>
          {students.map((s) => (
            <div key={String(s.id)} className="card" style={{ padding: 12 }}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
                <strong>{s.name}</strong>
                <span className="muted">{String((s as any).default_class_type || "1:1")}</span>
              </div>
              <div className="hint" style={{ marginTop: 6 }}>
                {String((s as any).venue || "—")}
              </div>
            </div>
          ))}
        </div>
      </div>
    </main>
  );
}

