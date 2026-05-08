import Link from "next/link";
import { readDb, todayKey } from "@/lib/db";

export default async function Home() {
  const db = await readDb();
  const today = todayKey();
  const todays = db.lessons
    .filter((l) => String(l?.start_at || "").startsWith(today))
    .slice()
    .sort((a, b) => String(a.start_at || "").localeCompare(String(b.start_at || "")));

  const studentById = new Map(db.students.map((s) => [String(s.id), s]));
  return (
    <main style={{ maxWidth: 980, margin: "0 auto", padding: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <h1 style={{ fontSize: 18, fontWeight: 700 }}>Coach Agent (Next.js)</h1>
        <div style={{ display: "flex", gap: 12 }}>
          <Link href="/">Home</Link>
        </div>
      </div>

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
