export type Sample = {at: number; messages: number; baseline: number; active: boolean; event?: string}
export type Channel = {
  id: string; name: string; platform: "Twitch" | "YouTube" | "Simulation"; avatar: string; url: string
  enabled: number; priority: number; offset: number; preset: string; samples: Sample[]
  discovery?: string; recording?: string; chat?: string; error?: string; health?: number; bitrate?: number
  mps?: number; chatWindow?: 5|10; raw5?: number; raw10?: number; raw15?: number; raw30?: number; score?: number; baseline?: number; unique5?: number
  comparedWindows?: number; similarWindows?: number
  ingestLag?: number | null; acceleration?: number; repeats?: number; emotes?: number; provisional?: boolean
  live?: {broadcast?: string; title: string; thumbnail?: string; viewers?: number | null}
}
export type Clip = {
  id: string; name: string; channel: string; platform: string; source: string; score: number; scoreVersion?: number; onset: number
  status: "ready" | "capturing" | "processing" | "failed"; review: string; favorite: number
  partial: number; duration: number; thumbnail: number; simulated: number; reason: string; offset: number
}
export type CutClip = {
  id: string; name: string; channel: string; preset: "original" | "vertical"
  created: number; duration: number | null; bytes: number
}
export type Settings = {
  slots: number; pre: number; post: number; maximum: number; buffer: number; quality: string
  preset: string; rise: number; deviations: number; baseline: number; onset: number; cooldown: number
  min_users: number; per_hour: number; disk_gb: number; notification: boolean; nvenc: boolean; output: string
  youtube_search_budget: number; youtube_units: number
}
export type State = {
  channels: Channel[]; clips: Clip[]; activities: {at: number; message: string; level: string}[]
  running: boolean; demo: boolean; settings: Settings
  system: {cpu: number; memory: number; processes: number; bytes: number; diskOk?: boolean}
  integration: {twitchConfigured: boolean; twitchAuth: string; twitchClientId: string; youtubeConfigured: boolean; youtubeAuth: string}
}
export type Analytics = {
  stats: Record<"events"|"clips"|"failed"|"score"|"bytes"|"duration"|"hours", number|null>
  groups: {channel: string; clips: number; bytes: number}[]
  peaks: Pick<Clip,"id"|"name"|"channel"|"onset"|"score"|"scoreVersion"|"status">[]
}
export async function api<T>(path: string, method = "GET", value?: unknown): Promise<T> {
  const response = await fetch("/api/" + path, {method, headers: {"Content-Type":"application/json","X-Blynx-Local":"1"},
    body: value === undefined ? undefined : JSON.stringify(value)})
  const data = await response.json()
  if (!response.ok) throw Error(typeof data.detail === "string" ? data.detail :
    Array.isArray(data.detail) ? data.detail.map((item: {msg: string}) => item.msg).join(". ") : "Request failed")
  return data as T
}

