import fs from "node:fs/promises";
import path from "node:path";
import { createClient } from "@supabase/supabase-js";

const url = process.env.SUPABASE_URL;
const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
if (!url || !key) {
  console.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY");
  process.exit(1);
}

const supa = createClient(url, key, { auth: { persistSession: false } });

const workspaceRoot = path.resolve(process.cwd(), "..");
const dbPath = path.join(workspaceRoot, "coach_data.json");
const raw = await fs.readFile(dbPath, "utf-8");
const db = JSON.parse(raw);

function arr(x) {
  return Array.isArray(x) ? x : [];
}

function s(x) {
  return x == null ? "" : String(x);
}

function n(x, fallback = 0) {
  const v = Number(x);
  return Number.isFinite(v) ? v : fallback;
}

async function upsert(table, rows) {
  if (!rows.length) return;
  const { error } = await supa.from(table).upsert(rows, { onConflict: "id" });
  if (error) throw new Error(`${table} upsert failed: ${error.message}`);
}

await upsert("students", arr(db.students));
await upsert("lesson_types", arr(db.lesson_types));
await upsert("coaches", arr(db.coaches));

// lessons: ensure jsonb fields are arrays
await upsert(
  "lessons",
  arr(db.lessons).map((l) => ({
    ...l,
    // satisfy NOT NULL constraints in schema
    class_type: s(l.class_type),
    currency: s(l.currency || "SGD"),
    venue: s(l.venue),
    status: s(l.status || "未说明"),
    next_plan: s(l.next_plan),
    original_text: s(l.original_text),
    coach_id: s(l.coach_id),
    start_at: s(l.start_at),
    created_at: s(l.created_at),
    updated_at: s(l.updated_at),
    baseline_minutes: l.baseline_minutes == null ? 120 : n(l.baseline_minutes, 120),
    price_per_lesson: n(l.price_per_lesson, 0),
    price_per_hour: n(l.price_per_hour, 0),
    participant_ids: Array.isArray(l.participant_ids) ? l.participant_ids : [],
    participant_names_raw: Array.isArray(l.participant_names_raw) ? l.participant_names_raw : [],
    topics: Array.isArray(l.topics) ? l.topics : [],
  }))
);

// payments: normalize to satisfy NOT NULL constraints
await upsert(
  "payments",
  arr(db.payments).map((p) => ({
    ...p,
    note: s(p.note),
    lesson_id: s(p.lesson_id),
    student_id: s(p.student_id),
    amount: n(p.amount, 0),
    currency: s(p.currency || "SGD"),
    paid: Boolean(p.paid),
    paid_at: p.paid_at == null ? null : s(p.paid_at),
    created_at: s(p.created_at),
    updated_at: s(p.updated_at),
  }))
);

console.log("Import done.");

