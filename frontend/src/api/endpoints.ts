import { request } from './client'
import type {
  Folder,
  FolderChoice,
  FolderDetail,
  Person,
  ActionItem,
  CalendarResponse,
  Job,
  Me,
  Analytics,
  AskResult,
  AskSavedTurn,
  Meeting,
  MeetingList,
  ScheduleResult,
  SeriesInfo,
  NotificationList,
  OptimalTime,
  Readiness,
  SearchResults,
  SessionInfo,
  SpeakerInfo,
  Summary,
  TaskList,
  TokenPair,
  TaskThread,
  ThreadEntry,
  Transcript,
  TranscriptSegment,
  UploadResult,
  VoiceProfile,
} from './types'

const V1 = '/api/v1'

export const authApi = {
  register: (email: string, password: string, name?: string) =>
    request<{ id: string; email: string; name: string | null }>('/auth/register', {
      method: 'POST',
      json: { email, password, name },
      auth: false,
    }),
  login: (email: string, password: string) =>
    request<TokenPair>('/auth/login', { method: 'POST', json: { email, password }, auth: false }),
  logout: (refreshToken: string | null) =>
    request<void>('/auth/logout', {
      method: 'POST',
      json: refreshToken ? { refresh_token: refreshToken } : {},
    }),
  logoutAll: () => request<void>('/auth/logout-all', { method: 'POST' }),
  me: () => request<Me>('/auth/me'),
  updateMe: (body: { name?: string; timezone?: string; email_notifications?: boolean }) =>
    request<Me>('/auth/me', { method: 'PATCH', json: body }),
  sessions: () => request<{ sessions: SessionInfo[] }>('/auth/sessions'),
  revokeSession: (id: string) => request<void>(`/auth/sessions/${id}`, { method: 'DELETE' }),
  changePassword: (current_password: string, new_password: string) =>
    request<TokenPair>('/auth/change-password', {
      method: 'POST',
      json: { current_password, new_password },
    }),
  forgotPassword: (email: string) =>
    request<{ detail: string }>('/auth/forgot-password', {
      method: 'POST',
      json: { email },
      auth: false,
    }),
  resetPassword: (token: string, new_password: string) =>
    request<{ detail: string }>('/auth/reset-password', {
      method: 'POST',
      json: { token, new_password },
      auth: false,
    }),
}

export interface MeetingQuery {
  status?: string
  participant?: string
  start_date?: string
  end_date?: string
  folder_id?: string
  limit?: number
  offset?: number
}

export interface ScheduleBody {
  title: string
  participants: string[]
  scheduled_time: string
  duration_minutes: number
  allow_conflicts?: boolean
  timezone?: string
  repeat?: string
  repeat_count?: number
  repeat_until?: string
  folder_id?: string | null
  new_folder?: string | null
}

export interface ProcessOptions {
  steps?: string[]
  language?: string
  identify_speakers?: boolean
  regenerate_summary?: boolean
}

export interface TaskQuery {
  status?: string
  assignee?: string
  meeting_id?: string
  due_from?: string
  due_to?: string
  has_due_date?: boolean
  /** Only tasks whose spoken deadline never became a date. */
  needs_review?: boolean
  /** Only tasks assigned to the signed-in user (true) or to others (false). */
  mine?: boolean
  sort?: 'due' | 'created'
  limit?: number
  offset?: number
}

export const meetingsApi = {
  list: (q: MeetingQuery = {}) => request<MeetingList>(`${V1}/meetings`, { query: { ...q } }),
  get: (id: string) => request<Meeting>(`${V1}/meetings/${id}`),
  upload: (form: FormData) =>
    request<UploadResult>(`${V1}/meetings/upload`, { method: 'POST', form }),
  schedule: (body: ScheduleBody) =>
    request<ScheduleResult>(`${V1}/meetings/schedule`, { method: 'POST', json: body }),
  update: (id: string, body: Partial<ScheduleBody>) =>
    request<Meeting>(`${V1}/meetings/${id}`, { method: 'PUT', json: body }),
  rename: (id: string, title: string) =>
    request<Meeting>(`${V1}/meetings/${id}/title`, { method: 'PATCH', json: { title } }),
  moveToFolder: (id: string, choice: FolderChoice) =>
    request<Meeting>(`${V1}/meetings/${id}/folder`, { method: 'PUT', json: choice }),
  cancel: (id: string, scope: 'this' | 'following' = 'this') =>
    request<Meeting & { cancelled_count?: number }>(`${V1}/meetings/${id}/cancel`, { method: 'POST', query: { scope: scope === 'this' ? undefined : scope } }),
  series: (id: string) => request<SeriesInfo>(`${V1}/meetings/${id}/series`),
  remove: (id: string) => request<void>(`${V1}/meetings/${id}`, { method: 'DELETE' }),

  process: (id: string, opts: ProcessOptions = {}) =>
    request<{ job_id: string; state: string; steps: string[]; status_url: string }>(
      `${V1}/meetings/${id}/process`,
      { method: 'POST', json: opts },
    ),
  job: (jobId: string) => request<Job>(`${V1}/jobs/${jobId}`),
  latestJob: (id: string) => request<Job>(`${V1}/meetings/${id}/jobs/latest`),

  transcript: (id: string) => request<Transcript>(`${V1}/meetings/${id}/transcript`),
  summary: (id: string) => request<Summary>(`${V1}/meetings/${id}/summary`),
  editSegment: (id: string, segmentId: number, text: string) =>
    request<TranscriptSegment & { changed: boolean }>(`${V1}/meetings/${id}/transcript/segments/${segmentId}`, {
      method: 'PATCH',
      json: { text },
    }),
  replaceText: (
    id: string,
    body: { find: string; replace: string; match_case?: boolean; whole_word?: boolean; include_summary?: boolean },
  ) =>
    request<{ transcript: number; summary: number; action_items: number; total: number }>(
      `${V1}/meetings/${id}/transcript/replace`,
      { method: 'POST', json: body },
    ),
  audio: (id: string) => request<Blob>(`${V1}/meetings/${id}/audio`, { as: 'blob' }),

  speakers: (id: string) =>
    request<{ meeting_id: string; speakers: SpeakerInfo[]; voices_available: boolean }>(`${V1}/meetings/${id}/speakers`),
  renameSpeaker: (id: string, body: { label: string; name: string; remember_voice?: boolean; email?: string; replace_heard?: string; revoke_previous?: boolean }) =>
    request<{ renamed: boolean; voice_saved: boolean; label: string; action_items_updated?: number; invited?: string; revoked?: string | null; text_fixed?: number }>(
      `${V1}/meetings/${id}/speakers`,
      { method: 'PUT', json: body },
    ),
  analyzeVoices: (id: string) =>
    request<{ job_id: string; state: string }>(`${V1}/meetings/${id}/speakers/analyze`, { method: 'POST' }),

  actions: (id: string) =>
    request<{ meeting_id: string; action_items: ActionItem[]; count: number }>(
      `${V1}/meetings/${id}/actions`,
    ),
  createAction: (
    id: string,
    body: { task: string; assignee?: string; due_date?: string | null; priority?: string },
  ) => request<ActionItem>(`${V1}/meetings/${id}/actions`, { method: 'POST', json: body }),
  updateAction: (
    id: string,
    actionId: string,
    body: {
      task?: string
      assignee?: string
      due_date?: string | null
      clear_due_date?: boolean
      priority?: string
      status?: string
    },
  ) => request<ActionItem>(`${V1}/meetings/${id}/actions/${actionId}`, { method: 'PUT', json: body }),
  action: (id: string, actionId: string) => request<ActionItem>(`${V1}/meetings/${id}/actions/${actionId}`),
  deleteAction: (id: string, actionId: string) =>
    request<{ deleted: boolean }>(`${V1}/meetings/${id}/actions/${actionId}`, { method: 'DELETE' }),
}

export const notificationsApi = {
  list: (limit = 30) => request<NotificationList>(`${V1}/notifications`, { query: { limit } }),
  markRead: (ids?: string[]) =>
    request<{ marked: number; unread: number }>(`${V1}/notifications/read`, { method: 'POST', json: ids ? { ids } : {} }),
  remove: (id: string) => request<void>(`${V1}/notifications/${id}`, { method: 'DELETE' }),
}

export const analyticsApi = {
  get: (days: number, signal?: AbortSignal) => request<Analytics>(`${V1}/analytics`, { query: { days }, signal }),
}

export const exportApi = {
  meetingCalendar: (id: string) => request<Blob>(`${V1}/meetings/${id}/calendar.ics`, { as: 'blob' }),
  calendar: (opts: { meetings?: boolean; tasks?: boolean; mine?: boolean } = {}) =>
    request<Blob>(`${V1}/export/calendar.ics`, { as: 'blob', query: { ...opts } }),
  features: () => request<{ email_enabled: boolean }>(`${V1}/features`),
  sendSummary: (id: string, body: { recipients?: string[]; note?: string }) =>
    request<{ sent: string[]; failed: string[] }>(`${V1}/meetings/${id}/send-summary`, { method: 'POST', json: body }),
}

export const searchApi = {
  search: (q: string, signal?: AbortSignal) => request<SearchResults>(`${V1}/search`, { query: { q }, signal }),
}

export const askApi = {
  ask: (
    body: { question: string; meeting_id?: string; folder_id?: string; history?: { question: string; answer: string }[] },
    signal?: AbortSignal,
  ) => request<AskResult>(`${V1}/ask`, { method: 'POST', json: body, signal }),
  history: (meetingId?: string, folderId?: string) =>
    request<{ turns: AskSavedTurn[] }>(`${V1}/ask/history`, { query: { meeting_id: meetingId, folder_id: folderId } }),
  clearHistory: (meetingId?: string, folderId?: string) =>
    request<void>(`${V1}/ask/history`, { method: 'DELETE', query: { meeting_id: meetingId, folder_id: folderId } }),
}

export const foldersApi = {
  list: () => request<{ count: number; folders: Folder[] }>(`${V1}/folders`),
  get: (id: string) => request<FolderDetail>(`${V1}/folders/${id}`),
  create: (name: string) => request<Folder>(`${V1}/folders`, { method: 'POST', json: { name } }),
  rename: (id: string, name: string) => request<Folder>(`${V1}/folders/${id}`, { method: 'PATCH', json: { name } }),
  remove: (id: string) => request<{ deleted: boolean; meetings_unfiled: number }>(`${V1}/folders/${id}`, { method: 'DELETE' }),
}

export const peopleApi = {
  list: () => request<{ count: number; people: Person[] }>(`${V1}/people`),
}

export const recordingApi = {
  /** Tell registered people in an online meeting that recording has started. */
  notify: (emails: string[]) =>
    request<{ notified: string[]; not_registered: string[] }>(`${V1}/recording-notice`, { method: 'POST', json: { emails } }),
}

export const voicesApi = {
  list: () => request<{ count: number; profiles: VoiceProfile[] }>(`${V1}/voice-profiles`),
  remove: (id: string) => request<void>(`${V1}/voice-profiles/${id}`, { method: 'DELETE' }),
  recordMine: (form: FormData) =>
    request<{ id: string; name: string; email: string; samples: number; speech_seconds: number }>(`${V1}/voice-profiles/me`, { method: 'POST', form }),
  setEmail: (id: string, email: string | null) =>
    request<VoiceProfile>(`${V1}/voice-profiles/${id}`, { method: 'PATCH', json: { email } }),
}

export const threadApi = {
  get: (meetingId: string, actionId: string) => request<TaskThread>(`${V1}/meetings/${meetingId}/actions/${actionId}/thread`),
  comment: (meetingId: string, actionId: string, body: string) =>
    request<ThreadEntry>(`${V1}/meetings/${meetingId}/actions/${actionId}/comments`, { method: 'POST', json: { body } }),
  removeComment: (meetingId: string, actionId: string, commentId: string) =>
    request<void>(`${V1}/meetings/${meetingId}/actions/${actionId}/comments/${commentId}`, { method: 'DELETE' }),
  requestMoreTime: (meetingId: string, actionId: string, requested_due: string, reason?: string) =>
    request<ThreadEntry>(`${V1}/meetings/${meetingId}/actions/${actionId}/extension`, { method: 'POST', json: { requested_due, reason } }),
  decide: (meetingId: string, actionId: string, requestId: string, approve: boolean, note?: string) =>
    request<ActionItem & TaskThread>(`${V1}/meetings/${meetingId}/actions/${actionId}/extension/${requestId}/decision`, {
      method: 'POST',
      json: { approve, note },
    }),
}

export const tasksApi = {
  list: (q: TaskQuery = {}) => request<TaskList>(`${V1}/actions`, { query: { ...q } }),
}

export interface CalendarQuery {
  start_date?: string
  end_date?: string
  event_type?: string
  status?: string
  meeting_id?: string
  max_results?: number
}

export const calendarApi = {
  events: (email: string, q: CalendarQuery = {}) =>
    request<CalendarResponse>(`${V1}/calendar/${encodeURIComponent(email)}/events`, {
      query: { max_results: 250, ...q },
    }),
  findTime: (body: {
    participants: string[]
    duration_minutes: number
    preferred_days?: string[]
    time_range?: Record<string, string>
  }) => request<OptimalTime>(`${V1}/calendar/find-optimal-time`, { method: 'POST', json: body }),
}

export const systemApi = {
  ready: async (): Promise<Readiness | null> => {
    try {
      // 503 still carries a useful body, so read it directly.
      const res = await fetch('/health/ready')
      return (await res.json()) as Readiness
    } catch {
      return null
    }
  },
}
