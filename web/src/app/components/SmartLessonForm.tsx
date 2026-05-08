"use client";

import { useMemo, useState } from "react";

type StudentRow = { id: string; name: string; default_class_type?: string; venue?: string };
type LessonTypePrice = { price_per_lesson: number; currency: string };

export function SmartLessonForm(props: {
  students: StudentRow[];
  venues: string[];
  lessonTypePrices: Record<string, LessonTypePrice>;
  onCreated?: () => void;
}) {
  const [studentId, setStudentId] = useState("");
  const [classType, setClassType] = useState("");
  const [venue, setVenue] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const studentById = useMemo(() => {
    const m = new Map<string, StudentRow>();
    for (const s of props.students) m.set(String(s.id), s);
    return m;
  }, [props.students]);

  const priceHint = useMemo(() => {
    const p = props.lessonTypePrices[classType];
    if (!classType || !p) return "";
    const amt = Number(p.price_per_lesson || 0);
    return `单价（每人/次）：${amt ? amt.toFixed(0) : "0"} ${p.currency || "SGD"}`;
  }, [classType, props.lessonTypePrices]);

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/lessons/smart", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, studentId, classType, venue }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data?.ok) {
        const msg = String(data?.error || "save_failed");
        throw new Error(msg);
      }
      setText("");
      props.onCreated?.();
    } catch (e: any) {
      setError(e?.message ? String(e.message) : "save_failed");
    } finally {
      setBusy(false);
    }
  }

  function onStudentChange(id: string) {
    setStudentId(id);
    const s = studentById.get(id);
    if (!s) return;
    const dct = String(s.default_class_type || "").replaceAll("：", ":").trim();
    setClassType(dct);
    setVenue(String(s.venue || ""));
  }

  return (
    <div className="card">
      <div className="row">
        <h2 style={{ margin: 0, fontSize: 16 }}>智能录入</h2>
        <button className="btn primary" onClick={submit} disabled={busy || !text.trim()}>
          {busy ? "保存中..." : "智能录入"}
        </button>
      </div>

      {error ? <div style={{ marginTop: 10, color: "#ff6b6b" }}>{error}</div> : null}

      <div style={{ marginTop: 12 }} className="grid2">
        <div className="field">
          <label>学员（可选）</label>
          <select value={studentId} onChange={(e) => onStudentChange(e.target.value)}>
            <option value="">不选择（由系统识别）</option>
            {props.students.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
          <div className="hint">
            {studentId ? `已选学员将覆盖解析；并自动填默认课程/场地。` : `不选择则只靠文本识别学员。`}
          </div>
        </div>

        <div className="field">
          <label>课程分类</label>
          <select value={classType} onChange={(e) => setClassType(e.target.value)}>
            <option value="">请选择</option>
            {(["1:1", "1:2", "1:3", "1:4"] as const).map((ct) => (
              <option key={ct} value={ct}>
                {ct}
              </option>
            ))}
          </select>
          {priceHint ? <div className="hint">{priceHint}</div> : null}
        </div>
      </div>

      <div style={{ marginTop: 12 }} className="grid2">
        <div className="field">
          <label>场地</label>
          <input list="venue_list" value={venue} onChange={(e) => setVenue(e.target.value)} placeholder="选择或输入场地" />
          <datalist id="venue_list">
            {props.venues.map((v) => (
              <option key={v} value={v} />
            ))}
          </datalist>
        </div>

        <div className="field">
          <label>一句话记录（必填）</label>
          <textarea value={text} onChange={(e) => setText(e.target.value)} placeholder="例如：今天Uma上课，练了高远球" />
        </div>
      </div>
    </div>
  );
}

