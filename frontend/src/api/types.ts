// Types mirroring the backend API responses.

export type MeetingStatus =
  | 'scheduled'
  | 'in_progress'
  | 'completed'
  | 'uploaded'
  | 'cancelled'
  | string

export type ActionStatus = 'pending' | 'in_progress' | 'completed'
export type Priority = 'low' | 'medium' | 'high'

export interface Me {
  id: string
  email: string
  /** Full name; null for accounts created before names were collected. */
  name: string | null
  /** IANA zone such as "Asia/Kolkata"; new meetings use it. */
  timezone?: string | null
  email_notifications?: boolean
  is_admin: boolean
  session_id: string
}

export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: 'bearer'
  expires_in: number
  refresh_expires_in: number
}

/** A group of one person's meetings; later meetings remember the earlier ones. */
export interface Folder {
  id: string
  name: string
  meeting_count: number
  last_meeting: string | null
  created_at: string | null
}

export interface FolderMeeting {
  number: number
  id: string
  title: string
  status: string
  when: string | null
  has_summary: boolean
  summary: string | null
  /** Labelled lines: how this meeting continues the earlier ones. */
  follow_up: string | null
  open_tasks: number
  done_tasks: number
}

export interface FolderDetail extends Folder {
  meetings: FolderMeeting[]
}

/** Where a new or moved meeting goes: one of your folders, or a new one made by name. */
export interface FolderChoice {
  folder_id?: string | null
  new_folder?: string | null
}

export interface Meeting {
  id: string
  title: string
  status: MeetingStatus
  scheduled_time: string | null
  end_time: string | null
  duration_minutes: number | null
  participants: string[]
  owner_email: string | null
  started_at: string | null
  ended_at: string | null
  has_audio: boolean
  /** False when the meeting belongs to someone else and was shared with you. */
  is_owner?: boolean
  owner_name?: string | null
  /** How the audio was made: upload | browser_mic | online_tab. */
  source?: string | null
  /** When the recorder confirmed everyone knew an online meeting was being recorded. */
  consent_at?: string | null
  /** The zone the scheduled wall-clock time is in, e.g. "Asia/Kolkata". */
  timezone?: string | null
  series_id?: string | null
  recurrence?: string | null
  /** The folder it is in (shown to the owner only), with its place in it (detail view only). */
  folder?: { id: string; name: string; number?: number | null; meeting_count?: number } | null
  /** Position in a repeating series (detail view only). */
  series?: { count: number; index: number | null } | null
  /** Real length of the stored recording (detail view only). */
  audio_seconds?: number | null
  created_at: string | null
  can_write?: boolean
  has_transcript?: boolean
  has_summary?: boolean
  action_count?: number
  open_action_count?: number
}

export interface MeetingList {
  count: number
  total: number
  meetings: Meeting[]
}

export interface UploadResult {
  meeting_id: string
  title: string
  status: string
  participants: string[]
  audio_filename: string
  audio_size_bytes: number
  audio_duration_seconds: number
  created_at: string
  folder?: { id: string; name: string } | null
}

export interface TranscriptSegment {
  id: number
  start_time: number
  end_time: number
  text: string
  speaker: string | null
  confidence: number
  /** Corrected by hand. */
  edited?: boolean
}

export interface Transcript {
  meeting_id: string
  transcript_id: string
  language: string
  transcript: string
  word_count: number
  speaker_count: number
  segments: TranscriptSegment[]
  created_at: string | null
}

export interface KeyPoint {
  topic: string
  description: string
}

export interface Decision {
  decision: string
  owner: string
  context: string
}

export interface Summary {
  meeting_id: string
  summary_id: string
  meeting_title: string
  executive_summary: string
  key_points: KeyPoint[]
  decisions: Decision[]
  next_steps: string[]
  key_quotes: string[]
  /** Labelled lines on how this meeting continues the earlier ones in its folder. */
  follow_up?: string | null
  created_at: string | null
}

export interface ActionItem {
  id: string
  task: string
  assignee: string | null
  due_date: string | null
  priority: Priority | string
  status: ActionStatus | string
  created_at: string | null
  completed_at: string | null
  /** The spoken deadline that could not become a date ("soon"); a person should choose a date. */
  due_phrase?: string | null
  needs_review?: boolean
  comment_count?: number
  /** A request for more time that is waiting for the owner's answer. */
  pending_extension?: { id: string; requested_due: string | null; by: { id: string | null; name: string | null; email: string | null } } | null
  /** The task is assigned to the signed-in user. */
  mine?: boolean
  /** The signed-in user may tick or reopen it (the assignee; the owner for unclaimed tasks). */
  can_update_status?: boolean
}

export interface TaskRow extends ActionItem {
  meeting_id: string
  meeting_title: string
  can_write: boolean
  mine: boolean
  can_update_status: boolean
}

export interface TaskList {
  count: number
  total: number
  action_items: TaskRow[]
}

export type JobState = 'queued' | 'running' | 'retrying' | 'succeeded' | 'failed'
export type StepState =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'skipped'
  | 'skipped_due_to_failure'

export interface JobStep {
  name: 'transcribe' | 'summarize' | 'extract_actions' | 'voices'
  state: StepState
  error: string | null
  result: Record<string, unknown> | null
}

export interface Job {
  job_id: string
  meeting_id: string
  state: JobState
  progress_percent: number
  attempts: number
  error: string | null
  steps: JobStep[]
  created_at: string
  updated_at: string
}

export interface CalendarEvent {
  id: string
  summary: string
  start: string
  end: string
  status: string
  type: 'meeting' | 'submission'
  meeting_id: string
  meeting_title?: string | null
  assignee?: string | null
  priority?: string | null
  date_source?: 'scheduled' | 'started' | 'recorded'
  duration_minutes?: number
  timezone?: string | null
  start_utc?: string | null
  end_utc?: string | null
  participants?: string[]
  calendar_id: string
}

export interface CalendarResponse {
  user_email: string
  calendar_id: string
  count: number
  total: number
  events: CalendarEvent[]
}

export interface Slot {
  start_time: string
  end_time: string
  confidence: number
  reasoning: string
}

export interface OptimalTime {
  optimal_time: string
  end_time: string
  confidence: number
  reasoning: string
  alternatives: Slot[]
}

export interface SessionInfo {
  id: string
  created_at: string
  last_used_at: string | null
  expires_at: string
  ip_address: string | null
  user_agent: string | null
  current: boolean
}

export interface Readiness {
  status: 'ready' | 'degraded' | 'not_ready'
  checks: { database: 'ok' | 'fail'; redis: 'ok' | 'fail'; ollama: 'ok' | 'fail' }
}

export interface ConflictInfo {
  id: string | null
  title: string
  scheduled_time: string | null
  end_time: string | null
  redacted?: boolean
}

export interface SpeakerClip {
  start: number
  end: number
  text: string
}

export interface SpeakerInfo {
  label: string
  segments: number
  seconds: number
  words: number
  /** True once the speaker has been given a real name. */
  named: boolean
  /** True when this speaker's voice can be remembered. */
  has_voice: boolean
  /** What the speaker called themselves ("My name is …"), if they did. */
  suggestion?: NameSuggestion | null
  /** A saved voice that sounds like this speaker: "Is this Rahul Patel?" */
  voice_match?: VoiceMatch | null
  /** A few of this speaker's turns to play, so they can be recognised by ear. */
  clips?: SpeakerClip[]
  /** For a named speaker: the sign-in e-mail of the member with that name, if there is one. */
  email?: string | null
}

export interface VoiceMatch {
  name: string
  /** 0-1: how alike the voices are. */
  score: number
  /** True when it was close enough to name automatically (an older meeting, or a voice saved afterwards). */
  strong: boolean
}

export interface NameSuggestion {
  /** The name to offer: the closest known person's spelling, else what was heard. */
  name: string
  /** The spelling in the transcript. */
  heard: string
  quote: string
  /** True when `name` is a corrected match to someone already known. */
  known: boolean
}

/** A member that can be picked when naming a speaker. */
export interface Person {
  name: string
  email: string
}

export interface VoiceProfile {
  id: string
  name: string
  /** Meetings where this voice is recognised are shared with this address. */
  email?: string | null
  samples: number
  created_at: string | null
}

export interface AskSource {
  tag: string
  meeting_id: string
  title: string
  date: string | null
  /** False when the answer cited nothing and these are just the meetings that were searched. */
  cited: boolean
}

export interface AskResult {
  answer: string
  sources: AskSource[]
  searched: number
  /** Present once the exchange has been saved to the conversation. */
  id?: string
  created_at?: string
}

export interface AskSavedTurn {
  id: string
  question: string
  answer: string
  sources: AskSource[]
  searched: number | null
  created_at: string
}

export interface SearchHit {
  kind: 'title' | 'summary' | 'task' | 'transcript'
  text: string
  speaker?: string | null
  segment_id?: number
  time?: number
  assignee?: string | null
  status?: string
  due?: string | null
}

export interface SearchMeeting {
  meeting_id: string
  title: string
  date: string | null
  hit_count: number
  hits: SearchHit[]
}

export interface SearchResults {
  query: string
  terms: string[]
  total: number
  meetings: SearchMeeting[]
}

export interface AnalyticsPerson {
  name: string
  talk_seconds: number
  talk_share: number
  words: number
  meetings: number
  tasks_total: number
  tasks_done: number
  tasks_open: number
  tasks_overdue: number
  completion_rate: number | null
}

export interface AnalyticsWeek {
  week_start: string
  meetings: number
  tasks_created: number
  tasks_completed: number
  talk_minutes: number
}

export interface Analytics {
  days: number
  overview: {
    meetings: number
    meetings_with_transcript: number
    recorded_seconds: number
    tasks: number
    tasks_done: number
    tasks_open: number
    tasks_overdue: number
    tasks_unassigned_open: number
    completion_rate: number | null
    unnamed_talk_seconds: number
  }
  open_by_priority: { high: number; medium: number; low: number }
  people: AnalyticsPerson[]
  weeks: AnalyticsWeek[]
}

export interface AppNotification {
  id: string
  kind: string
  title: string
  body: string | null
  link: string | null
  meeting_id: string | null
  created_at: string
  read: boolean
}

export interface NotificationList {
  unread: number
  items: AppNotification[]
}

export interface ThreadEntry {
  id: string
  kind: 'comment' | 'extension_request'
  body: string | null
  author: { id: string | null; name: string | null; email: string | null }
  mine: boolean
  created_at: string
  requested_due: string | null
  status: 'pending' | 'approved' | 'declined' | null
  decided_by: { id: string | null; name: string | null; email: string | null } | null
  decided_at: string | null
  decision_note: string | null
}

export interface TaskThread {
  comments: ThreadEntry[]
  can_comment: boolean
  can_request_extension: boolean
  can_decide: boolean
}

export interface SeriesInfo {
  series_id: string
  recurrence: string
  label: string
  meetings: { id: string; scheduled_time: string | null; status: string; is_current: boolean }[]
}

/** What scheduling a repeating meeting reports. */
export interface ScheduleResult extends Omit<Meeting, 'series'> {
  series?: { id: string; recurrence: string; created: number; skipped: { scheduled_time: string; reason: string }[] }
}
