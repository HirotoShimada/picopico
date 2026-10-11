import { sectionLabel } from "@/components/audio/sections";

/**
 * Japanese names of the storyboard vocabulary (`presets.SCENE_TYPES`,
 * `CAMERA_ANGLES`, `CAMERA_MOVES`). Unknown ids are shown as-is.
 */
const SCENE_TYPE_LABELS: Record<string, string> = {
  establishing_wide: "情景ワイド",
  walking: "歩く",
  back_view: "後ろ姿",
  close_portrait: "表情のアップ",
  profile_window: "窓辺の横顔",
  turn_to_light: "振り返り",
  running: "走る",
  wind_hair: "風を受ける",
  silhouette: "シルエット",
  spin_sway: "揺れる・回る",
  crowd_still: "雑踏の中で",
  train_window: "車窓",
  rooftop: "屋上",
  lying_down: "寝そべる",
  water_reflection: "水面の反射",
  ambient_detail: "質感ディテール",
  sky_drift: "空",
  light_abstract: "光の抽象",
  nature_macro: "自然のマクロ",
  street_flow: "街の流れ",
};

const CAMERA_ANGLE_LABELS: Record<string, string> = {
  eye_level: "目線の高さ",
  low_angle: "ローアングル",
  high_angle: "ハイアングル",
  overhead: "真俯瞰",
  over_shoulder: "肩越し",
  ground_level: "地面すれすれ",
  through_foreground: "前景越し",
  dutch_tilt: "ダッチアングル",
};

const CAMERA_MOVE_LABELS: Record<string, string> = {
  static: "フィックス",
  slow_push_in: "ゆっくり寄る",
  slow_pull_out: "ゆっくり引く",
  pan_slow: "ゆっくりパン",
  tilt_up: "ティルトアップ",
  rack_focus: "ピント送り",
  handheld: "手持ち",
  tracking_side: "横移動",
  orbit: "回り込み",
  crane_up: "クレーンアップ",
  follow_behind: "追従",
  drone_rise: "ドローン上昇",
  fast_push: "勢いよく寄る",
};

export function sceneTypeLabel(id: string | null | undefined): string | null {
  return id ? (SCENE_TYPE_LABELS[id] ?? id) : null;
}

export function cameraAngleLabel(id: string | null | undefined): string | null {
  return id ? (CAMERA_ANGLE_LABELS[id] ?? id) : null;
}

export function cameraMoveLabel(id: string | null | undefined): string | null {
  return id ? (CAMERA_MOVE_LABELS[id] ?? id) : null;
}

export function songSectionLabel(id: string | null | undefined): string | null {
  return id ? sectionLabel(id) : null;
}

const PAN_LABELS: Record<string, string> = {
  left: "左へパン",
  right: "右へパン",
  up: "上へパン",
  down: "下へパン",
  center: "中央",
};

export function panLabel(pan: string): string {
  return PAN_LABELS[pan] ?? pan;
}

const START_STRATEGY_LABELS: Record<string, string> = {
  segment: "選択した区間",
  chorus: "サビから",
  varied: "動画ごとに変える",
  custom: "指定位置",
};

export function startStrategyLabel(id: string): string {
  return START_STRATEGY_LABELS[id] ?? id;
}

const REUSE_LABELS: Record<string, string> = {
  max_variety: "バリエーション最大",
  balanced: "バランス",
  economy: "節約",
};

export function reuseLabel(id: string): string {
  return REUSE_LABELS[id] ?? id;
}

/** `GenerationSettings.allow_mirror` (左右反転). */
export function mirrorLabel(allowMirror: boolean): string {
  return allowMirror ? "使う" : "使わない";
}
