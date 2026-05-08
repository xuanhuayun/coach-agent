import { supabaseAdmin } from "@/lib/supabaseAdmin";
import { readDb } from "@/lib/db";
import type { Student } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function StudentsPage() {
  const supa = supabaseAdmin();
  let students: Student[] = [];
  const supabaseConfigured = !!process.env.SUPABASE_URL && !!process.env.SUPABASE_SERVICE_ROLE_KEY;
  let source: "supabase" | "local" = supa ? "supabase" : "local";
  let loadError: string | null = null;
  if (supa) {
    const { data, error } = await supa.from("students").select("*");
    if (error) loadError = `${error.message}`;
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
        <div className="hint" style={{ marginTop: 8 }}>
          数据来源：{source === "supabase" ? "Supabase" : "本地 coach_data.json"} · 学员数：{students.length}
        </div>
        {!supabaseConfigured ? (
          <div style={{ marginTop: 10, color: "#ff6b6b" }}>
            未配置 Supabase 环境变量（`SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY`）。Vercel 上会退回本地数据，但线上没有
            `coach_data.json`，所以会显示空。
          </div>
        ) : null}
        {loadError ? (
          <div style={{ marginTop: 10, color: "#ff6b6b" }}>Supabase 读取失败：{loadError}</div>
        ) : null}
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

