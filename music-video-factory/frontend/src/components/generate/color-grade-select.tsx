"use client";

import * as React from "react";

import { Select, SelectContent, SelectItem, SelectSeparator, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useCapabilities } from "@/hooks/use-capabilities";
import type { ColorGradeInfo } from "@/lib/types";

import { COLOR_GRADE_SWATCHES, COLOR_GRADES_FALLBACK } from "./text-style";

export function useColorGrades(): readonly ColorGradeInfo[] {
  const { data } = useCapabilities();
  return data?.color_grades.length ? data.color_grades : COLOR_GRADES_FALLBACK;
}

export function GradeSwatch({ id, className }: { id: string; className?: string }) {
  const colors = COLOR_GRADE_SWATCHES[id];
  if (!colors) return <span className={className} aria-hidden />;
  return (
    <span className={className} aria-hidden>
      <span
        className="block h-3.5 w-8 rounded-sm border border-white/10"
        style={{ backgroundImage: `linear-gradient(90deg, ${colors[0]} 0%, ${colors[1]} 50%, ${colors[2]} 100%)` }}
      />
    </span>
  );
}

export interface ColorGradeSelectProps {
  id?: string;
  value: string;
  onValueChange: (value: string) => void;
  /** Offer "auto" (vary per video from the preset) — batch settings only. */
  includeAuto?: boolean;
  disabled?: boolean;
  size?: "sm" | "md";
}

export function ColorGradeSelect({ id, value, onValueChange, includeAuto = false, disabled, size }: ColorGradeSelectProps) {
  const grades = useColorGrades();
  return (
    <Select value={value} onValueChange={onValueChange} disabled={disabled}>
      <SelectTrigger id={id} size={size} aria-label="カラーグレード">
        <SelectValue placeholder="カラーグレードを選択" />
      </SelectTrigger>
      <SelectContent>
        {includeAuto ? (
          <>
            <SelectItem value="auto" description="世界観に合うグレードを動画ごとに変えます">
              自動（動画ごとに変化）
            </SelectItem>
            <SelectSeparator />
          </>
        ) : null}
        {grades.map((grade) => (
          <SelectItem key={grade.id} value={grade.id}>
            <span className="flex items-center gap-2">
              <GradeSwatch id={grade.id} />
              {grade.name}
            </span>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
