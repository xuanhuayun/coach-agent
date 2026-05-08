export type ClassTypeCode = "1:1" | "1:2" | "1:3" | "1:4";

export type Student = {
  id: string;
  name: string;
  contact?: string;
  note?: string;
  venue?: string;
  created_at?: string;
  archived?: boolean;
  default_class_type?: string;
};

export type LessonType = {
  id: string;
  code: string;
  label?: string;
  participants?: number;
  price_per_lesson?: number;
  currency?: string;
  active?: boolean;
};

export type Lesson = {
  id: string;
  coach_id?: string;
  lesson_type_id?: string | null;
  class_type?: string;
  baseline_minutes?: number;
  price_per_lesson?: number;
  currency?: string;
  start_at?: string;
  duration_minutes?: number | null;
  participant_ids?: string[];
  participant_names_raw?: string[];
  draft?: boolean;
  topics?: string[];
  status?: string;
  next_plan_time?: string | null;
  next_plan?: string;
  original_text?: string;
  venue?: string;
  created_at?: string;
  updated_at?: string;
};

export type Coach = {
  id: string;
  name?: string;
  contact?: string;
  created_at?: string;
  venues?: string[];
};

export type Payment = {
  id: string;
  lesson_id?: string;
  student_id?: string;
  amount?: number;
  currency?: string;
  paid?: boolean;
  paid_at?: string | null;
  created_at?: string;
  updated_at?: string;
};

export type CoachDb = {
  schema_version: number;
  students: Student[];
  lessons: Lesson[];
  lesson_types: LessonType[];
  payments: Payment[];
  coaches: Coach[];
  finance_reports?: unknown;
};

