/**
 * Typed browser client for the FastAPI backend (ARCHITECTURE.md §5).
 *
 * The browser talks to the backend directly at `NEXT_PUBLIC_API_BASE_URL`.
 * In `AUTH_MODE=token` the user stores a token on /settings (localStorage) and
 * every API request carries `Authorization: Bearer <token>`. Signed media URLs
 * (`/media/...`) need no header.
 */

import type {
  AnalysisOut,
  AnalysisRerunOut,
  AutoTimeOut,
  AutoTimeRequest,
  BatchDetailOut,
  BatchOut,
  BatchUpdate,
  CapabilitiesOut,
  ClipOut,
  DashboardOut,
  Estimate,
  GenerationSettingsInput,
  HealthOut,
  LyricLine,
  LyricLinesOut,
  LyricsExportFormat,
  LyricsOut,
  LyricsParseOut,
  LyricsUpdate,
  ProjectCreate,
  ProjectOut,
  ProjectSummary,
  ProjectUpdate,
  RightsUpdate,
  SegmentOut,
  SegmentUpdate,
  Storyboard,
  StoryboardRequest,
  VideoDetailOut,
  VideoEdit,
  VideoListFilters,
  VideoOut,
  VideoRegenerateRequest,
  VideoUpdate,
  WorldEstimate,
} from "./types";
import { navigateToDownload, saveBlob } from "./utils";

// --- configuration -------------------------------------------------------------------

const DEFAULT_API_BASE_URL = "http://localhost:8000";

export const API_BASE_URL: string = (process.env.NEXT_PUBLIC_API_BASE_URL?.trim() || DEFAULT_API_BASE_URL).replace(
  /\/+$/,
  "",
);

// --- token storage -------------------------------------------------------------------

const TOKEN_STORAGE_KEY = "mvf.apiToken";
const TOKEN_CHANGE_EVENT = "mvf:api-token-change";

/** The Bearer token saved on /settings, or `null` (also during SSR / when storage is blocked). */
export function getApiToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const value = window.localStorage.getItem(TOKEN_STORAGE_KEY);
    return value && value.trim() ? value.trim() : null;
  } catch {
    return null;
  }
}

/** Save (or clear with `null` / empty) the Bearer token and notify subscribers. */
export function setApiToken(token: string | null): void {
  if (typeof window === "undefined") return;
  const value = token?.trim() ?? "";
  try {
    if (value) window.localStorage.setItem(TOKEN_STORAGE_KEY, value);
    else window.localStorage.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    // Storage blocked (private mode / policy): the token simply isn't persisted.
  }
  window.dispatchEvent(new Event(TOKEN_CHANGE_EVENT));
}

/** Subscribe to token changes from this tab and other tabs (for `useSyncExternalStore`). */
export function subscribeApiToken(callback: () => void): () => void {
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || event.key === TOKEN_STORAGE_KEY) callback();
  };
  window.addEventListener(TOKEN_CHANGE_EVENT, callback);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(TOKEN_CHANGE_EVENT, callback);
    window.removeEventListener("storage", onStorage);
  };
}

// --- errors ----------------------------------------------------------------------------

const STATUS_MESSAGES: Record<number, string> = {
  400: "リクエストの内容が正しくありません。",
  401: "認証が必要です。設定画面で API トークンを設定してください。",
  403: "この操作を行う権限がありません。",
  404: "対象が見つかりません。",
  405: "この操作はサポートされていません。",
  409: "現在の状態ではこの操作を実行できません。",
  413: "ファイルサイズが上限を超えています。",
  415: "対応していないファイル形式です。",
  422: "入力内容に誤りがあります。",
  429: "リクエストが集中しています。しばらく待ってから再試行してください。",
  500: "サーバーでエラーが発生しました。",
  502: "外部サービスとの通信に失敗しました。",
  503: "サービスが一時的に利用できません。",
  504: "サーバーの応答がタイムアウトしました。",
};

/** Generic English messages from FastAPI/Starlette that we replace with Japanese. */
const GENERIC_ENGLISH_DETAILS = new Set([
  "not found",
  "method not allowed",
  "not authenticated",
  "unauthorized",
  "forbidden",
  "internal server error",
  "bad request",
  "request entity too large",
  "payload too large",
  "service unavailable",
  "bad gateway",
  "gateway timeout",
  "too many requests",
  "invalid authentication credentials",
]);

const FIELD_LABELS: Record<string, string> = {
  title: "タイトル",
  artist_name: "アーティスト名",
  mood: "雰囲気",
  purpose: "動画生成目的",
  notes: "メモ",
  world_preset: "世界観プリセット",
  custom_world: "カスタム世界観",
  budget_limit_usd: "予算上限",
  file: "ファイル",
  files: "ファイル",
  text: "テキスト",
  format: "形式",
  lines: "歌詞行",
  start: "開始位置",
  end: "終了位置",
  method: "方式",
  preset: "プリセット",
  provider: "プロバイダー",
  model: "モデル",
  duration_sec: "動画の長さ",
  count: "本数",
  lyric_style: "歌詞スタイル",
  color_grade: "カラーグレード",
  start_strategy: "開始位置の決め方",
  start_sec: "開始秒",
  reuse: "素材の再利用",
  max_new_clips: "新規生成クリップ上限",
  seed: "シード",
  items: "権利確認",
  video_ids: "動画",
  favorite: "お気に入り",
  scenes: "シーン数",
};

interface ValidationIssue {
  type?: string;
  loc?: (string | number)[];
  msg?: string;
  ctx?: Record<string, unknown>;
}

function fieldLabel(loc: (string | number)[] | undefined): string {
  if (!loc || loc.length === 0) return "入力";
  const parts = loc.filter((part) => part !== "body" && part !== "query" && part !== "path");
  const name = [...parts].reverse().find((part): part is string => typeof part === "string");
  if (!name) return "入力";
  return FIELD_LABELS[name] ?? name;
}

function describeIssue(issue: ValidationIssue): string {
  const field = fieldLabel(issue.loc);
  const ctx = issue.ctx ?? {};
  switch (issue.type) {
    case "missing":
      return `${field}は必須です`;
    case "string_too_short":
      return ctx.min_length === 1 ? `${field}を入力してください` : `${field}は${ctx.min_length}文字以上にしてください`;
    case "string_too_long":
      return `${field}は${ctx.max_length}文字以内にしてください`;
    case "greater_than_equal":
      return `${field}は${ctx.ge}以上にしてください`;
    case "greater_than":
      return `${field}は${ctx.gt}より大きい値にしてください`;
    case "less_than_equal":
      return `${field}は${ctx.le}以下にしてください`;
    case "less_than":
      return `${field}は${ctx.lt}未満にしてください`;
    case "too_short":
      return `${field}を1件以上指定してください`;
    case "too_long":
      return `${field}の件数が多すぎます`;
    case "int_parsing":
    case "int_type":
    case "float_parsing":
    case "float_type":
      return `${field}は数値で入力してください`;
    case "bool_parsing":
    case "bool_type":
      return `${field}の値が正しくありません`;
    case "literal_error":
    case "enum":
      return `${field}の値が選択肢にありません`;
    case "string_pattern_mismatch":
      return `${field}の形式が正しくありません`;
    case "json_invalid":
      return "リクエストの形式が正しくありません";
    case "value_error":
    case "assertion_error":
      return (issue.msg ?? "").replace(/^(Value|Assertion) error,\s*/i, "") || `${field}の値が正しくありません`;
    default:
      return issue.msg ? `${field}: ${issue.msg}` : `${field}の値が正しくありません`;
  }
}

function hasJapanese(text: string): boolean {
  return /[぀-ヿ㐀-鿿＀-￯]/.test(text);
}

/** Build the user-facing (Japanese) message for an HTTP error response. */
export function messageForError(status: number, detail: unknown): string {
  const fallback = STATUS_MESSAGES[status] ?? `エラーが発生しました（HTTP ${status}）。`;
  if (typeof detail === "string" && detail.trim()) {
    const text = detail.trim();
    if (hasJapanese(text)) return text;
    if (GENERIC_ENGLISH_DETAILS.has(text.toLowerCase())) return fallback;
    return `${fallback}（${text}）`;
  }
  if (Array.isArray(detail) && detail.length > 0) {
    const messages = Array.from(new Set((detail as ValidationIssue[]).map(describeIssue))).slice(0, 5);
    return messages.join("。") + "。";
  }
  if (detail && typeof detail === "object" && "message" in detail) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string" && message.trim()) return messageForError(status, message);
  }
  return fallback;
}

export class ApiError extends Error {
  /** HTTP status; `0` when the backend could not be reached. */
  readonly status: number;
  /** Machine-readable error code from the backend (`{"code": ...}`), if any. */
  readonly code: string | null;
  /** Raw `detail` from the response body. */
  readonly detail: unknown;
  readonly method: string;
  readonly url: string;

  constructor(init: { status: number; message: string; code?: string | null; detail?: unknown; method: string; url: string }) {
    super(init.message);
    this.name = "ApiError";
    this.status = init.status;
    this.code = init.code ?? null;
    this.detail = init.detail ?? null;
    this.method = init.method;
    this.url = init.url;
  }

  get isNetworkError(): boolean {
    return this.status === 0;
  }

  get isUnauthorized(): boolean {
    return this.status === 401 || this.status === 403;
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }

  static network(method: string, url: string): ApiError {
    return new ApiError({
      status: 0,
      code: "network_error",
      message: `バックエンド（${API_BASE_URL}）に接続できません。サーバーが起動しているか確認してください。`,
      method,
      url,
    });
  }

  static fromBody(status: number, body: unknown, method: string, url: string): ApiError {
    const record = body && typeof body === "object" ? (body as Record<string, unknown>) : {};
    const detail = "detail" in record ? record.detail : typeof body === "string" ? body : null;
    const code = typeof record.code === "string" ? record.code : null;
    return new ApiError({ status, code, detail, message: messageForError(status, detail), method, url });
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

/** True when the backend could not be reached at all (connection refused, DNS, CORS). */
export function isBackendUnreachable(error: unknown): boolean {
  return isApiError(error) && error.isNetworkError;
}

/** True for user/programmatic cancellation (`AbortController.abort()`). */
export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/** Japanese message for any thrown value (falls back to a generic message). */
export function errorMessage(error: unknown): string {
  if (isApiError(error)) return error.message;
  if (isAbortError(error)) return "キャンセルしました。";
  if (error instanceof Error && error.message && hasJapanese(error.message)) return error.message;
  return "予期しないエラーが発生しました。";
}

// --- low-level request helpers ------------------------------------------------------------

export type QueryValue = string | number | boolean | null | undefined;
export type Query = Record<string, QueryValue>;

/** Append non-empty query params to a path (keeps the result usable as an SWR key). */
export function withQuery(path: string, query?: Query): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue;
    params.append(key, String(value));
  }
  const search = params.toString();
  return search ? `${path}${path.includes("?") ? "&" : "?"}${search}` : path;
}

/** Absolute backend URL for an API path such as `/api/projects`. */
export function apiUrl(path: string, query?: Query): string {
  if (/^https?:\/\//.test(path)) return withQuery(path, query);
  return `${API_BASE_URL}${withQuery(path.startsWith("/") ? path : `/${path}`, query)}`;
}

function authHeaders(): Record<string, string> {
  const token = getApiToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  query?: Query;
  /** JSON body (serialized with `JSON.stringify`). */
  json?: unknown;
  /** Raw body (e.g. `FormData`); mutually exclusive with `json`. */
  body?: BodyInit;
  signal?: AbortSignal;
  headers?: Record<string, string>;
}

async function parseBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  const type = response.headers.get("content-type") ?? "";
  if (type.includes("json")) {
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }
  return text;
}

/** Perform a request and return the raw `Response` (throws `ApiError` for non-2xx). */
export async function apiFetch(path: string, options: RequestOptions = {}): Promise<Response> {
  const method = options.method ?? "GET";
  const url = apiUrl(path, options.query);
  const headers: Record<string, string> = { Accept: "application/json", ...authHeaders(), ...options.headers };
  let body = options.body;
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.json);
  }

  let response: Response;
  try {
    response = await fetch(url, { method, headers, body, signal: options.signal, cache: "no-store" });
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw ApiError.network(method, url);
  }
  if (!response.ok) {
    throw ApiError.fromBody(response.status, await parseBody(response).catch(() => null), method, url);
  }
  return response;
}

/** Perform a JSON request; resolves to `undefined` for 204 responses. */
export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await apiFetch(path, options);
  if (response.status === 204) return undefined as T;
  return (await parseBody(response)) as T;
}

/** SWR fetcher: the key is an API path (see `apiPaths`). */
export function swrFetcher<T>(path: string): Promise<T> {
  return apiRequest<T>(path);
}

export interface UploadProgress {
  loaded: number;
  total: number;
  /** 0..1 of the request body sent. 1 means the server is now processing. */
  fraction: number;
}

export interface UploadOptions {
  onProgress?: (progress: UploadProgress) => void;
  signal?: AbortSignal;
}

/**
 * POST a multipart form with upload progress (XMLHttpRequest — `fetch` has no
 * upload progress events). Rejects with `ApiError`, or an `AbortError`
 * `DOMException` when `signal` aborts.
 */
export function uploadWithProgress<T>(path: string, form: FormData, options: UploadOptions = {}): Promise<T> {
  const url = apiUrl(path);
  const { onProgress, signal } = options;

  return new Promise<T>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("アップロードを中止しました", "AbortError"));
      return;
    }
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.setRequestHeader("Accept", "application/json");
    for (const [name, value] of Object.entries(authHeaders())) xhr.setRequestHeader(name, value);

    const onAbortSignal = () => xhr.abort();
    signal?.addEventListener("abort", onAbortSignal, { once: true });
    const cleanup = () => signal?.removeEventListener("abort", onAbortSignal);

    xhr.upload.onprogress = (event) => {
      if (!onProgress) return;
      const total = event.lengthComputable ? event.total : 0;
      onProgress({ loaded: event.loaded, total, fraction: total > 0 ? Math.min(1, event.loaded / total) : 0 });
    };
    xhr.upload.onload = () => {
      onProgress?.({ loaded: 1, total: 1, fraction: 1 });
    };
    xhr.onload = () => {
      cleanup();
      const contentType = xhr.getResponseHeader("content-type") ?? "";
      let body: unknown = xhr.responseText;
      if (contentType.includes("json") && xhr.responseText) {
        try {
          body = JSON.parse(xhr.responseText);
        } catch {
          // keep raw text
        }
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as T);
      else reject(ApiError.fromBody(xhr.status, body, "POST", url));
    };
    xhr.onerror = () => {
      cleanup();
      reject(ApiError.network("POST", url));
    };
    xhr.onabort = () => {
      cleanup();
      reject(new DOMException("アップロードを中止しました", "AbortError"));
    };
    xhr.send(form);
  });
}

function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  const star = /filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)/.exec(header);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ""));
    } catch {
      // fall through to the ASCII name
    }
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/.exec(header);
  return plain ? plain[1].trim() : null;
}

/** Fetch an authenticated binary/text response and save it as a file. */
export async function downloadAuthenticated(path: string, fallbackName: string, options: RequestOptions = {}): Promise<void> {
  const response = await apiFetch(path, { ...options, headers: { Accept: "*/*", ...options.headers } });
  const blob = await response.blob();
  saveBlob(blob, filenameFromDisposition(response.headers.get("content-disposition")) ?? fallbackName);
}

// --- paths (also the SWR keys) ---------------------------------------------------------------

const enc = encodeURIComponent;

export const apiPaths = {
  health: "/api/health",
  capabilities: "/api/system/capabilities",
  dashboard: "/api/dashboard",
  projects: "/api/projects",
  project: (projectId: string) => `/api/projects/${enc(projectId)}`,
  projectRights: (projectId: string) => `/api/projects/${enc(projectId)}/rights`,
  projectAudio: (projectId: string) => `/api/projects/${enc(projectId)}/audio`,
  analysis: (projectId: string) => `/api/projects/${enc(projectId)}/analysis`,
  worldEstimate: (projectId: string) => `/api/projects/${enc(projectId)}/world-estimate`,
  analysisRerun: (projectId: string) => `/api/projects/${enc(projectId)}/analysis/rerun`,
  segment: (projectId: string) => `/api/projects/${enc(projectId)}/segment`,
  lyrics: (projectId: string) => `/api/projects/${enc(projectId)}/lyrics`,
  lyricsUpload: (projectId: string) => `/api/projects/${enc(projectId)}/lyrics/upload`,
  lyricsLines: (projectId: string) => `/api/projects/${enc(projectId)}/lyrics/lines`,
  lyricsAutoTime: (projectId: string) => `/api/projects/${enc(projectId)}/lyrics/auto-time`,
  lyricsExport: (projectId: string, format: LyricsExportFormat) =>
    withQuery(`/api/projects/${enc(projectId)}/lyrics/export`, { format }),
  clips: (projectId: string) => `/api/projects/${enc(projectId)}/clips`,
  clipsUpload: (projectId: string) => `/api/projects/${enc(projectId)}/clips/upload`,
  clip: (clipId: string) => `/api/clips/${enc(clipId)}`,
  storyboard: (projectId: string) => `/api/projects/${enc(projectId)}/storyboard`,
  estimate: (projectId: string) => `/api/projects/${enc(projectId)}/estimate`,
  projectBatches: (projectId: string) => `/api/projects/${enc(projectId)}/batches`,
  batch: (batchId: string) => `/api/batches/${enc(batchId)}`,
  batchCancel: (batchId: string) => `/api/batches/${enc(batchId)}/cancel`,
  batchZip: (batchId: string) => `/api/batches/${enc(batchId)}/zip`,
  videosZip: "/api/videos/zip",
  projectVideos: (projectId: string, filters: VideoListFilters = {}) =>
    withQuery(`/api/projects/${enc(projectId)}/videos`, {
      favorite: filters.favorite,
      batch_id: filters.batch_id,
    }),
  video: (videoId: string) => `/api/videos/${enc(videoId)}`,
  videoRetry: (videoId: string) => `/api/videos/${enc(videoId)}/retry`,
  videoRegenerate: (videoId: string) => `/api/videos/${enc(videoId)}/regenerate`,
  videoEdit: (videoId: string) => `/api/videos/${enc(videoId)}/edit`,
  videoDownload: (videoId: string) => `/api/videos/${enc(videoId)}/download`,
} as const;

// --- §5.1 system & dashboard -------------------------------------------------------------------

export function getHealth(signal?: AbortSignal): Promise<HealthOut> {
  return apiRequest(apiPaths.health, { signal });
}

export function getCapabilities(): Promise<CapabilitiesOut> {
  return apiRequest(apiPaths.capabilities);
}

export function getDashboard(): Promise<DashboardOut> {
  return apiRequest(apiPaths.dashboard);
}

// --- §5.2 projects ---------------------------------------------------------------------------------

export function listProjects(): Promise<ProjectSummary[]> {
  return apiRequest(apiPaths.projects);
}

export function createProject(body: ProjectCreate): Promise<ProjectOut> {
  return apiRequest(apiPaths.projects, { method: "POST", json: body });
}

export function getProject(projectId: string): Promise<ProjectOut> {
  return apiRequest(apiPaths.project(projectId));
}

export function updateProject(projectId: string, patch: ProjectUpdate): Promise<ProjectOut> {
  return apiRequest(apiPaths.project(projectId), { method: "PATCH", json: patch });
}

export function deleteProject(projectId: string): Promise<void> {
  return apiRequest(apiPaths.project(projectId), { method: "DELETE" });
}

export function updateRights(projectId: string, items: RightsUpdate["items"]): Promise<ProjectOut> {
  return apiRequest(apiPaths.projectRights(projectId), { method: "PUT", json: { items } satisfies RightsUpdate });
}

// --- §5.3 audio & analysis ------------------------------------------------------------------------------

/** Upload (or replace) the project's song. Resolves to the updated project (analysis queued). */
export function uploadAudio(projectId: string, file: File, options: UploadOptions = {}): Promise<ProjectOut> {
  const form = new FormData();
  form.append("file", file, file.name);
  return uploadWithProgress(apiPaths.projectAudio(projectId), form, options);
}

export function getAnalysis(projectId: string): Promise<AnalysisOut> {
  return apiRequest(apiPaths.analysis(projectId));
}

/** 感情・世界観推定 from the audio mood, lyrics and the project's mood/purpose. */
export function getWorldEstimate(projectId: string): Promise<WorldEstimate> {
  return apiRequest(apiPaths.worldEstimate(projectId));
}

export function rerunAnalysis(projectId: string): Promise<AnalysisRerunOut> {
  return apiRequest(apiPaths.analysisRerun(projectId), { method: "POST" });
}

export function updateSegment(projectId: string, segment: SegmentUpdate): Promise<SegmentOut> {
  return apiRequest(apiPaths.segment(projectId), { method: "PUT", json: segment });
}

// --- §5.4 lyrics -------------------------------------------------------------------------------------------

export function updateLyrics(projectId: string, body: LyricsUpdate): Promise<LyricsParseOut> {
  return apiRequest(apiPaths.lyrics(projectId), { method: "PUT", json: { format: "auto", ...body } });
}

/** Upload a .txt / .lrc / .srt lyrics file (≤ 1 MB; UTF-8, UTF-8 BOM or Shift_JIS). */
export function uploadLyrics(projectId: string, file: File, options: UploadOptions = {}): Promise<LyricsParseOut> {
  const form = new FormData();
  form.append("file", file, file.name);
  return uploadWithProgress(apiPaths.lyricsUpload(projectId), form, options);
}

export function getLyrics(projectId: string): Promise<LyricsOut> {
  return apiRequest(apiPaths.lyrics(projectId));
}

export function updateLyricLines(projectId: string, lines: LyricLine[]): Promise<LyricLinesOut> {
  return apiRequest(apiPaths.lyricsLines(projectId), { method: "PUT", json: { lines } });
}

/** Auto-time lyric lines. Whisper unavailable → `ApiError` with status 503. */
export function autoTimeLyrics(projectId: string, request: AutoTimeRequest): Promise<AutoTimeOut> {
  return apiRequest(apiPaths.lyricsAutoTime(projectId), { method: "POST", json: request });
}

/** Exported LRC / SRT text. */
export async function exportLyrics(projectId: string, format: LyricsExportFormat): Promise<string> {
  const response = await apiFetch(apiPaths.lyricsExport(projectId, format), { headers: { Accept: "text/plain" } });
  return response.text();
}

/** Download the LRC / SRT export as a file (works in token auth mode). */
export function downloadLyricsExport(projectId: string, format: LyricsExportFormat, filename?: string): Promise<void> {
  return downloadAuthenticated(apiPaths.lyricsExport(projectId, format), filename ?? `lyrics.${format}`);
}

// --- §5.5 clips ----------------------------------------------------------------------------------------------

export function listClips(projectId: string): Promise<ClipOut[]> {
  return apiRequest(apiPaths.clips(projectId));
}

/** Upload 1..20 video materials (all-or-nothing on the server). */
export function uploadClips(projectId: string, files: File[], options: UploadOptions = {}): Promise<ClipOut[]> {
  const form = new FormData();
  for (const file of files) form.append("files", file, file.name);
  return uploadWithProgress(apiPaths.clipsUpload(projectId), form, options);
}

export function deleteClip(clipId: string): Promise<void> {
  return apiRequest(apiPaths.clip(clipId), { method: "DELETE" });
}

// --- §5.6 storyboard, estimate, batches -------------------------------------------------------------------------

/** Storyboard preview (not persisted). */
export function previewStoryboard(projectId: string, request: StoryboardRequest): Promise<Storyboard> {
  return apiRequest(apiPaths.storyboard(projectId), { method: "POST", json: request });
}

export function estimateBatch(projectId: string, settings: GenerationSettingsInput, signal?: AbortSignal): Promise<Estimate> {
  return apiRequest(apiPaths.estimate(projectId), { method: "POST", json: settings, signal });
}

/** Start a generation batch. 422 when audio/rights/provider/budget preconditions fail. */
export function createBatch(projectId: string, settings: GenerationSettingsInput): Promise<BatchOut> {
  return apiRequest(apiPaths.projectBatches(projectId), { method: "POST", json: settings });
}

export function listBatches(projectId: string): Promise<BatchOut[]> {
  return apiRequest(apiPaths.projectBatches(projectId));
}

export function getBatch(batchId: string): Promise<BatchDetailOut> {
  return apiRequest(apiPaths.batch(batchId));
}

export function cancelBatch(batchId: string): Promise<BatchOut> {
  return apiRequest(apiPaths.batchCancel(batchId), { method: "POST" });
}

/** New budget cap for a batch (e.g. to resume a `budget_exceeded` one; its stopped videos can then be retried). */
export function updateBatchBudget(batchId: string, body: BatchUpdate): Promise<BatchDetailOut> {
  return apiRequest(apiPaths.batch(batchId), { method: "PATCH", json: body });
}

/** Absolute URL of the batch ZIP (needs no header only in `AUTH_MODE=dev`). */
export function batchZipUrl(batchId: string): string {
  return apiUrl(apiPaths.batchZip(batchId));
}

/**
 * Download all finished videos of a batch as ZIP. Without a token the browser
 * streams it natively; with a token it is fetched with the Bearer header.
 */
export async function downloadBatchZip(batchId: string, filename = `batch_${batchId}.zip`): Promise<void> {
  if (!getApiToken()) {
    navigateToDownload(batchZipUrl(batchId));
    return;
  }
  await downloadAuthenticated(apiPaths.batchZip(batchId), filename);
}

/** Download a ZIP of the selected videos (`POST /api/videos/zip`). */
export function downloadVideosZip(videoIds: string[], filename = "videos.zip"): Promise<void> {
  return downloadAuthenticated(apiPaths.videosZip, filename, { method: "POST", json: { video_ids: videoIds } });
}

// --- §5.7 videos --------------------------------------------------------------------------------------------------

export function listVideos(projectId: string, filters: VideoListFilters = {}): Promise<VideoOut[]> {
  return apiRequest(apiPaths.projectVideos(projectId, filters));
}

export function getVideo(videoId: string): Promise<VideoDetailOut> {
  return apiRequest(apiPaths.video(videoId));
}

export function updateVideo(videoId: string, patch: VideoUpdate): Promise<VideoOut> {
  return apiRequest(apiPaths.video(videoId), { method: "PATCH", json: patch });
}

/** Re-render the same plan (failed / cancelled videos). */
export function retryVideo(videoId: string): Promise<VideoOut> {
  return apiRequest(apiPaths.videoRetry(videoId), { method: "POST" });
}

/** New plan variant (new seed), optionally with newly generated clips, then re-render. */
export function regenerateVideo(videoId: string, body: VideoRegenerateRequest = {}): Promise<VideoOut> {
  return apiRequest(apiPaths.videoRegenerate(videoId), { method: "POST", json: body });
}

/** Save manual edit overrides and re-render (increments `render_version`). */
export function editVideo(videoId: string, edit: VideoEdit): Promise<VideoOut> {
  return apiRequest(apiPaths.videoEdit(videoId), { method: "PUT", json: edit });
}

/** Absolute URL of `GET /api/videos/{id}/download` (302 → signed attachment URL). */
export function videoDownloadUrl(videoId: string): string {
  return apiUrl(apiPaths.videoDownload(videoId));
}

/**
 * Resolve the signed attachment URL behind the download redirect. Only the
 * response headers are awaited; the body request is aborted immediately.
 */
export async function resolveVideoDownloadUrl(videoId: string): Promise<string> {
  const controller = new AbortController();
  const response = await apiFetch(apiPaths.videoDownload(videoId), {
    signal: controller.signal,
    headers: { Accept: "*/*" },
  });
  const finalUrl = response.url;
  controller.abort();
  return finalUrl;
}

/** Download one rendered MP4 (`{artist}_{title}_{index}.mp4`). */
export async function downloadVideo(videoId: string): Promise<void> {
  if (!getApiToken()) {
    navigateToDownload(videoDownloadUrl(videoId));
    return;
  }
  navigateToDownload(await resolveVideoDownloadUrl(videoId));
}
