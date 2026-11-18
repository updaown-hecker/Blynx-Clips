import { useEffect, useState, type FormEvent, type ReactNode } from "react"
import { createRoot } from "react-dom/client"
import { AnimatePresence, motion } from "motion/react"
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis, ReferenceDot } from "recharts"
import {
  Activity, ArrowDownToLine, ArrowRight, AudioLines, BarChart3, Bell, CircleHelp,
  Clapperboard, Clock3, Cpu, ExternalLink, FlaskConical, FolderOpen, HardDrive, LayoutDashboard,
  LayoutGrid, Link2, List, Monitor, Play, Plus, Puzzle, Radio, RefreshCw, Search, Settings2, Square,
  Scissors, Star, Trash2, Twitch, Users, Video, Wifi, X, Youtube, Zap
} from "lucide-react"
import { Button } from "./button"
import { api, type Analytics, type Channel, type Clip, type CutClip, type Settings, type State } from "./types"
import "./style.css"

const pages = [
  {name:"Dashboard", icon:LayoutDashboard}, {name:"Streamers",icon:Users}, {name:"Live Monitoring",icon:Radio},
  {name:"Highlights",icon:Clapperboard}, {name:"Cut Clips",icon:Scissors}, {name:"Analytics",icon:BarChart3},
  {name:"Chrome Extension",icon:Puzzle}, {name:"Settings",icon:Settings2},
] as const
type Page = typeof pages[number]["name"]
type Task = (operation: () => Promise<unknown>, done?: string) => void
const number = new Intl.NumberFormat(undefined, {maximumFractionDigits:1})
const time = (timestamp: number) => new Date(timestamp * 1000).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"})
const date = (timestamp: number) => new Date(timestamp * 1000).toLocaleString([], {dateStyle:"medium",timeStyle:"short"})
const size = (bytes: number) => bytes >= 1e9 ? number.format(bytes / 1e9) + " GB" : number.format(bytes / 1e6) + " MB"
const isActive = (channel: Channel) => ["Recording","Buffering","Starting recorder"].includes(channel.recording ?? "")

function scoreLabel(score:number, version?:number) {
  return version === 2 ? number.format(score) + "/100" : number.format(score) + " (legacy)"
}

function Platform({name, width=15}: {name:string; width?:number}) {
  return name === "Twitch" ? <Twitch size={width} className="twitch"/> : name === "YouTube" ?
    <Youtube size={width} className="youtube"/> : <FlaskConical size={width} className="accent"/>
}

function Pill({children, tone="muted"}:{children:ReactNode; tone?:string}) {
  return <span className={"pill tone-" + tone}><span className="status-dot"/>{children}</span>
}

function Metric({icon, label, value, note}: {icon:ReactNode; label:string; value:ReactNode; note?:string}) {
  return <div className="metric"><span className="metric-icon">{icon}</span><div><div className="label">{label}</div>
    <div className="metric-value">{value}</div>{note && <div className="metric-note">{note}</div>}</div></div>
}

function Empty({icon, title, children}: {icon:ReactNode; title:string; children?:ReactNode}) {
  return <div className="empty"><div className="empty-icon">{icon}</div><h3>{title}</h3>{children}</div>
}

function Graph({channel, onEvent}: {channel:Channel; onEvent:(id:string)=>void}) {
  const data = channel.samples
  const last = data.at(-1)
  if (!last) return <div className="graph-empty"><div className="gridlines"/><span><AudioLines size={15}/> Waiting for chat activity</span></div>
  return <div className="chart"><ResponsiveContainer width="100%" height="100%">
    <AreaChart data={data} margin={{top:14,left:-19,right:11,bottom:1}}>
      <defs><linearGradient id={"chart-" + channel.id} x1="0" x2="0" y1="0" y2="1"><stop stopColor="#7678ff" stopOpacity={.38}/>
        <stop offset="100%" stopColor="#7678ff" stopOpacity={.01}/></linearGradient></defs>
      <XAxis dataKey="at" type="number" domain={["dataMin","dataMax"]} tickFormatter={time} tickCount={4}
        axisLine={false} tickLine={false} tick={{fill:"#9295a9",fontSize:10}}/>
      <YAxis axisLine={false} tickLine={false} tick={{fill:"#9295a9",fontSize:10}}/>
      <Tooltip labelFormatter={label=>time(Number(label))} contentStyle={{background:"#1c1d2b",border:"1px solid #35364a",borderRadius:8,fontSize:12}}/>
      <Area type="monotone" name="Baseline" dataKey="baseline" fill="transparent" stroke="#575c80" strokeDasharray="4 5" isAnimationActive={false}/>
      <Area type="monotone" name={`Messages / ${channel.chatWindow ?? 5}s`} dataKey="messages" fill={"url(#chart-" + channel.id + ")"} stroke="#9291ff" strokeWidth={2} isAnimationActive={false}/>
      {data.map((sample)=>sample.event && <ReferenceDot key={sample.event} x={sample.at} y={sample.messages} r={5} fill="#f5b460"
       stroke="#432d23" style={{cursor:"pointer"}} onClick={()=>onEvent(sample.event!)}/>)}
    </AreaChart></ResponsiveContainer></div>
}

function StreamCard({channel, clips, onEvent, task, detailed=false}: {channel:Channel; clips:Clip[]; onEvent:(id:string)=>void; task:Task; detailed?:boolean}) {
  const live = Boolean(channel.live)
  return <motion.article layout className="stream-card">
    <div className="stream-cover">{channel.live?.thumbnail && <img src={channel.live.thumbnail} alt=""/>}
      <div className="stream-cover-inner"><Platform name={channel.platform} width={24}/>
        <Pill tone={live ? "red" : "muted"}>{live ? (channel.platform === "Simulation" ? "Simulation" : "Live") : channel.discovery ?? "Waiting"}</Pill>
      </div></div>
    <div className="stream-body">
      <div className="stream-head">
        <div className="avatar">{channel.avatar ? <img alt="" src={channel.avatar}/> : <Platform name={channel.platform} width={19}/>}</div>
        <div className="grow"><h3>{channel.name}</h3><div className="secondary truncate" title={channel.live?.title}>{channel.live?.title ?? channel.platform}</div></div>
        {channel.live?.viewers != null && <span className="viewers"><Users size={13}/>{number.format(channel.live.viewers)}</span>}
      </div>
      <div className="stream-stats"><div><small>Chat / {channel.chatWindow ?? 5}s</small><b>{(channel.chatWindow === 10 ? channel.raw10 : channel.raw5) ?? "—"}</b></div>
        <div><small>Baseline</small><b>{channel.baseline == null ? "—" : number.format(channel.baseline)}</b></div>
        <div><small>Spike score</small><b>{channel.score == null ? "—" : number.format(channel.score) + "/100"}</b></div>
        <div><small>Clips</small><b>{clips.filter(c=>c.channel===channel.name && c.status==="ready").length}</b></div></div>
      <Graph channel={channel} onEvent={onEvent}/>
      <div className="stream-bottom"><Pill tone={isActive(channel) ? "green" : "muted"}>{channel.recording ?? (live ? "Queued" : "Standby")}</Pill>
        <span><HardDrive size={13}/> {channel.health == null ? "Buffer —" : channel.health + "s buffered"}</span></div>
      {isActive(channel)&&<Button variant="ghost" disabled={(channel.health??0)<5} onClick={()=>
        task(()=>api("channels/"+channel.id+"/clip","POST"),"Saving footage to Highlights")}>Save recent footage</Button>}
      <div className="stream-detail" title={channel.error ?? channel.chat}>
        {channel.error ?? channel.chat ?? "Chat connection starts with recording"}
      </div>
      {detailed && <div className="detailed">
        <div>1s / 15s / 30s <b>{channel.mps ?? "—"} / {channel.raw15 ?? "—"} / {channel.raw30 ?? "—"}</b></div>
        <div title={`Earlier ${channel.chatWindow ?? 5}-second windows with at least this much chat activity`}>Similar or busier windows
          <b>{(channel.comparedWindows ?? 0) < 12 ? "Collecting history" :
            channel.similarWindows + " of " + channel.comparedWindows}</b></div>
        {channel.chatWindow !== 10 && <div>Unique chatters (5s) <b>{channel.unique5 ?? "—"}</b></div>}<div>Acceleration <b>{channel.acceleration ?? "—"}</b></div>
        <div>Repeats / emotes (1s) <b>{channel.repeats ?? "—"} / {channel.emotes ?? "—"}</b></div>
        <div>Recording bitrate <b>{channel.bitrate ? number.format(channel.bitrate/1e6)+" Mbps" : "—"}</b></div>
        <div>Chat transport lag <b>{channel.ingestLag != null ? channel.ingestLag+"s" : "—"}</b></div>
        <div>Timing offset <b>{channel.offset}s</b></div><div>Timing <b>{channel.platform === "Simulation" ? "Synthetic" : "Estimated"}{channel.provisional ? " · warming up" : ""}</b></div>
      </div>}
    </div>
  </motion.article>
}

function ChannelRow({channel, task}: {channel:Channel; task:Task}) {
  const patch = (values:Record<string,unknown>)=>task(()=>api("channels/"+channel.id,"PATCH",values),"Streamer updated")
  return <article className="channel-row">
    <div className="avatar">{channel.avatar ? <img alt="" src={channel.avatar}/> : <Platform name={channel.platform}/>}</div>
    <div className="grow"><b>{channel.name}</b><div className="secondary truncate">{channel.discovery ?? channel.platform}</div></div>
    <label>Priority<input type="number" aria-label="Priority" min={0} max={100} key={channel.priority}
       defaultValue={channel.priority} onBlur={event=> { const value=+event.target.value; if(value!==channel.priority) patch({priority:value}) }}/></label>
    <label>Offset (s)<input type="number" aria-label="Timing offset" min={-40} max={40} step=".5" key={channel.offset}
       defaultValue={channel.offset} onBlur={event=> { const value=+event.target.value; if(value!==channel.offset) patch({offset:value}) }}/></label>
    <label>Sensitivity<select value={channel.preset} onChange={event=>patch({preset:event.target.value})}>
      <option value="">Global</option>{["Low","Balanced","High","Manual"].map(p=><option key={p}>{p}</option>)}</select></label>
    <Button onClick={()=>patch({enabled:!channel.enabled})}>{channel.enabled?"Pause":"Enable"}</Button>
    <Button variant="danger" aria-label="Remove streamer" title="Remove streamer" onClick={()=> {
      if(confirm("Remove " + channel.name + " from monitoring?")) task(()=>api("channels/"+channel.id,"DELETE"),"Streamer removed")
    }}><Trash2 size={16}/></Button>
  </article>
}

function ClipLibrary({clips, open}: {clips:Clip[]; open:(id:string)=>void}) {
  const [query,setQuery]=useState("")
  const [platform,setPlatform]=useState("")
  const [sort,setSort]=useState("new")
  const [duration,setDuration]=useState("")
  const [list,setList]=useState(false)
  const [favorite,setFavorite]=useState(false)
  const filtered=clips.filter(c=>(!platform||c.platform===platform)&&(!favorite||c.favorite)&&(!duration|| (duration==="short"?c.duration<=60:c.duration>60))&&
    (c.name+" "+c.channel).toLowerCase().includes(query.toLowerCase())).sort((a,b)=>sort==="score"?(b.scoreVersion??1)-(a.scoreVersion??1)||b.score-a.score:b.onset-a.onset)
  return <><div className="toolbar">
    <label className="search"><Search size={15}/><input value={query} placeholder="Search highlights" onChange={e=>setQuery(e.target.value)}/></label>
    <select aria-label="Platform" value={platform} onChange={e=>setPlatform(e.target.value)}><option value="">All platforms</option>
      {["Twitch","YouTube","Simulation"].map(p=><option key={p}>{p}</option>)}</select>
    <select aria-label="Duration" value={duration} onChange={e=>setDuration(e.target.value)}><option value="">Any duration</option>
      <option value="short">Up to 60s</option><option value="long">Over 60s</option></select>
    <select aria-label="Sort highlights" value={sort} onChange={e=>setSort(e.target.value)}><option value="new">Newest first</option><option value="score">Spike score</option></select>
    <Button aria-pressed={favorite} aria-label="Favorites only" onClick={()=>setFavorite(!favorite)}><Star size={15} fill={favorite?"currentColor":"none"}/></Button>
    <Button aria-label={list?"Grid view":"List view"} onClick={()=>setList(!list)}>{list?<LayoutGrid size={16}/>:<List size={16}/>}</Button>
  </div>
  {filtered.length ? <div className={list?"clip-grid clip-list":"clip-grid"}>{filtered.map(c=><button key={c.id} className="clip-card" onClick={()=>open(c.id)}>
    <div className="clip-cover">{Boolean(c.thumbnail) ? <img src={"/media/"+c.id+"/thumbnail"} alt=""/> : <Clapperboard size={27}/>}
      {c.duration>0&&<span>{number.format(c.duration)}s</span>}</div>
    <div className="clip-body"><strong title={c.name}>{c.name}</strong><span><Platform name={c.platform}/>{c.channel} · {time(c.onset)}</span>
      <span className="clip-score"><Zap size={12}/>{c.score>0?scoreLabel(c.score,c.scoreVersion):"Manual"} · {c.status==="ready"?c.review:c.status}{c.partial?" · partial":""}</span></div>
  </button>)}</div> : <Empty icon={<Clapperboard/>} title={clips.length?"No highlights match these filters":"Your highlights will appear here"}>
    <p>{clips.length?"Change your search or filters to see more.":"Clips appear as soon as FFmpeg finishes processing them."}</p></Empty>}</>
}

function CutDownload({id}: {id:string}) {
  return <Button asChild><a href={`/media/cut-clips/${id}/download`} download>
    <ArrowDownToLine size={15}/>Download MP4
  </a></Button>
}

function CutClips({openHighlights}: {openHighlights:()=>void}) {
  const [cuts,setCuts]=useState<CutClip[]>()
  const [error,setError]=useState("")
  const [attempt,retry]=useState(0)
  useEffect(()=>{
    let active=true
    api<CutClip[]>("cut-clips").then(result=>{
      if(active) setCuts(result)
    }).catch(error=>{
      if(active) setError(error.message)
    })
    return ()=>{active=false}
  },[attempt])
  if(error) return <Empty icon={<Scissors/>} title="Could not load cut clips">
    <p>{error}</p>
    <Button onClick={()=>{setError("");retry(attempt+1)}}>Try again</Button>
  </Empty>
  if(!cuts) return <p role="status" className="secondary">Loading cut clips…</p>
  if(!cuts.length) return <Empty icon={<Scissors/>} title="No cut clips yet">
    <p>Open a highlight, set a trim range, then choose Save cut clip.</p>
    <Button onClick={openHighlights}>Open Highlights <ArrowRight size={14}/></Button>
  </Empty>
  return <section className="panel" aria-label="Saved cut clips">{cuts.map(cut=>
    <div key={cut.id} className="channel-row">
      <span className="avatar"><Scissors size={17}/></span>
      <div className="grow"><div className="truncate" title={cut.name}><strong>{cut.name}</strong></div>
        <p className="secondary">{[cut.channel, cut.preset==="vertical"?"Vertical 9:16":"Original aspect",
          cut.duration===null?"":number.format(cut.duration)+"s", size(cut.bytes), date(cut.created)].filter(Boolean).join(" · ")}</p>
      </div>
      <CutDownload id={cut.id}/>
    </div>
  )}</section>
}

function ClipDetail({clip, close, task, openCuts}: {clip:Clip;close:()=>void;task:Task;openCuts:()=>void}) {
  const [name,setName]=useState(clip.name)
  const [start,setStart]=useState("0")
  const [end,setEnd]=useState("")
  const [preset,setPreset]=useState("original")
  const [busy,setBusy]=useState(false)
  const [exported,setExported]=useState("")
  const patch=(value:Record<string,unknown>)=>task(()=>api("clips/"+clip.id,"PATCH",value),"Highlight updated")
  return <Dialog title={clip.name} close={close} wide>
    {clip.status==="ready" ? <video className="video" controls playsInline src={"/media/"+clip.id+"/video"}/> :
      <div className="notice">{clip.status==="failed"?clip.reason:"Clip is being created. This view will update automatically."}</div>}
    <div className="clip-info"><Platform name={clip.platform}/>{clip.channel} · {date(clip.onset)} · {clip.score>0?"score "+scoreLabel(clip.score,clip.scoreVersion):"manual capture"}
      {clip.partial?" · partial footage":""}</div>
    {clip.reason && <p className="secondary">{clip.reason} · timing offset {clip.offset}s</p>}
    <div className="toolbar"><input aria-label="Highlight name" value={name} onChange={e=>setName(e.target.value)}/>
      <Button onClick={()=>patch({name})} disabled={!name.trim()||name===clip.name}>Rename</Button>
      <select aria-label="Review status" value={clip.review} onChange={e=>patch({review:e.target.value})}>
        {["New","Reviewed","Approved","Exported","Rejected"].map(r=><option key={r}>{r}</option>)}</select>
      <Button aria-label="Toggle favorite" onClick={()=>patch({favorite:!clip.favorite})}><Star size={15} fill={clip.favorite?"currentColor":"none"}/></Button>
    </div>
    {clip.status==="ready"&&<><div className="toolbar">
      <Button asChild><a href={"/media/"+clip.id+"/download"}><ArrowDownToLine size={15}/>Download MP4</a></Button>
      <Button onClick={()=>task(()=>api("clips/"+clip.id+"/location","POST"))}><FolderOpen size={15}/>Open folder</Button>
      {clip.source&&<Button onClick={()=>task(()=>navigator.clipboard.writeText(clip.source),"Source link copied")}><Link2 size={15}/>Copy source</Button>}
    </div><div className="export-box"><h3>Trim clip</h3><div className="toolbar"><label>Trim start (s)<input type="number" min={0} max={clip.duration}
        step=".1" value={start} onChange={e=>setStart(e.target.value)}/></label>
      <label>Trim end (s)<input type="number" min={1} max={clip.duration} step=".1" value={end} placeholder={clip.duration.toFixed(1)} onChange={e=>setEnd(e.target.value)}/></label>
      <label>Format<select value={preset} onChange={e=>setPreset(e.target.value)}><option value="original">Original aspect</option><option value="vertical">1080×1920 center crop</option></select></label>
      <Button variant="primary" disabled={busy} onClick={()=>task(async()=>{
        setBusy(true)
        try {
          const result=await api<{id:string}>("clips/"+clip.id+"/export","POST",{start:+start,end:end?+end:null,preset})
          setExported(result.id)
        }
        finally {setBusy(false)}
      },"Saved to Cut Clips")}>{busy?"Encoding…":"Save cut clip"}</Button></div>
      {exported&&<div className="toolbar">
        <CutDownload id={exported}/>
        <Button variant="ghost" onClick={openCuts}>View Cut Clips <ArrowRight size={14}/></Button>
      </div>}
    </div></>}
    {["ready","failed"].includes(clip.status)&&<Button variant="danger" onClick={()=>{
      if(confirm("Delete this highlight and its saved video?")) task(async()=>{await api("clips/"+clip.id,"DELETE"); close()},"Highlight deleted")
    }}><Trash2 size={15}/>Delete highlight</Button>}
  </Dialog>
}

function Dialog({title, children, close, wide=false}: {title:string;children:ReactNode;close:()=>void;wide?:boolean}) {
  useEffect(()=>{const listener=(e:KeyboardEvent)=>{if(e.key==="Escape") close()};window.addEventListener("keydown",listener);return ()=>window.removeEventListener("keydown",listener)},[close])
  return <motion.div className="dialog-layer" initial={{opacity:0}} animate={{opacity:1}} exit={{opacity:0}} onClick={close}>
    <motion.section role="dialog" aria-modal="true" aria-label={title} className={wide?"dialog dialog-wide":"dialog"} initial={{scale:.98,y:10}}
      animate={{scale:1,y:0}} transition={{type:"spring",stiffness:360,damping:30}} onClick={e=>e.stopPropagation()}>
      <header><h2 title={title}>{title}</h2><Button variant="ghost" aria-label="Close dialog" onClick={close}><X size={20}/></Button></header>
      {children}</motion.section></motion.div>
}

function AddChannel({close, refresh, setup}: {close:()=>void;refresh:()=>void;setup:()=>void}) {
  const [url,setUrl]=useState("")
  const [error,setError]=useState("")
  const [pending,setPending]=useState(false)
  const save=async (event:FormEvent)=>{
    event.preventDefault();setPending(true);setError("")
    try {await api("channels","POST",{url});refresh();close()} catch(error){setError(String(error instanceof Error?error.message:error))} finally {setPending(false)}
  }
  return <Dialog title="Add a streamer" close={close}><form onSubmit={save} className="add-form">
    <label htmlFor="channel-link">Twitch or YouTube URL</label><input autoFocus id="channel-link" autoComplete="off"
      value={url} onChange={e=>setUrl(e.target.value)} placeholder="https://www.twitch.tv/channel"/>
    <p className="secondary">Use a channel link to watch future livestreams or a YouTube watch link to track one broadcast.</p>
    {error && <p className="error" role="alert">{error}</p>}
    {error && /credential|API key|Authoriz|token/i.test(error) && <Button type="button" variant="ghost" onClick={setup}>Open connection settings <ArrowRight size={14}/></Button>}
    <Button variant="primary" disabled={pending||!url.trim()}><Plus size={16}/>{pending?"Validating…":"Save streamer"}</Button>
  </form></Dialog>
}

const fields: [keyof Settings,string,string][] = [
  ["slots","Concurrent recordings","1–3"],["buffer","Buffer seconds","90–600"],["pre","Pre-roll seconds","0–60"],
  ["post","Post-roll seconds","2–90"],["maximum","Maximum clip seconds","15–300"],["disk_gb","Storage limit (GB)","Pauses recording when reached"],
  ["rise","Relative spike sensitivity","Manual preset"],["deviations","Minimum statistical deviation","Manual preset"],["baseline","Baseline samples","30–900"],
  ["onset","Spike onset seconds","1–12"],["cooldown","Cooldown seconds","6–600"],["min_users","Minimum distinct chatters","2–500"],
  ["per_hour","Clips per hour / stream","1–60"],["youtube_search_budget","YouTube searches / day","Shared across saved channels"],
  ["youtube_units","Other YouTube units / day","Discovery and live chat"],
]
function Connections({integration, task}: {integration:State["integration"];task:Task}) {
  const [clientId, setClientId] = useState(integration.twitchClientId)
  const [twitchToken, setTwitchToken] = useState("")
  const [youtubeValue, setYouTubeValue] = useState("")
  const [mode, setMode] = useState("api-key")
  const [device, setDevice] = useState<{user_code:string;verification_uri:string}>()
  const [busy, setBusy] = useState("")
  const perform = (name:string, action:()=>Promise<unknown>, confirmation?:string) => {
    setBusy(name)
    task(async()=>{try {await action()} finally {setBusy("")}},confirmation)
  }
  const saveTwitch = () => api("integrations/twitch","PUT",{client_id:clientId.trim(),token:twitchToken.trim()})
  return <section className="panel connections">
    <h2>Platform connections</h2>
    <div className="setup-grid">
      <form className="provider" onSubmit={event=>{
        event.preventDefault()
        perform("twitch",async()=>{await saveTwitch();setTwitchToken("");setDevice(undefined)},twitchToken?"Twitch token verified and saved":"Twitch Client ID saved")
      }}>
        <header><span className="provider-icon purple"><Twitch size={20}/></span><div><h3>Twitch</h3>
          <p className="secondary" role="status">{integration.twitchAuth || (integration.twitchConfigured?"Client ID saved":"Connect to monitor Twitch chat")}</p></div></header>
        <label>Twitch Client ID<input value={clientId} onChange={e=>setClientId(e.target.value)} required autoComplete="off" minLength={15}/></label>
        <label>Twitch user access token <small>Optional if you sign in below</small>
          <input type="password" value={twitchToken} onChange={e=>setTwitchToken(e.target.value)} autoComplete="new-password" placeholder="Paste a token with user:read:chat"/></label>
        <div className="toolbar"><Button variant="primary" type="button" disabled={Boolean(busy)||clientId.trim().length<15} onClick={()=>
          perform("twitch",async()=>{
            if(clientId.trim()!==integration.twitchClientId)
              await api("integrations/twitch","PUT",{client_id:clientId.trim()})
            setDevice(await api("integrations/twitch/authorize","POST"))
          })}>Sign in with Twitch</Button>
          <Button type="submit" disabled={Boolean(busy)||!twitchToken.trim()}>Verify and save token</Button>
          {integration.twitchConfigured&&<Button type="button" variant="ghost" disabled={Boolean(busy)} onClick={()=>
            perform("twitch",()=>api("integrations/twitch/test","POST"),"Twitch authorization verified")}>Test</Button>}
        </div>
        {device&&integration.twitchAuth!=="Twitch connected"&&<div className="notice"><div>Enter code <strong>{device.user_code}</strong></div>
          <a href={device.verification_uri} target="_blank" rel="noreferrer">Open Twitch activation <ExternalLink size={13}/></a></div>}
        <a className="provider-help" href="https://dev.twitch.tv/console/apps" target="_blank" rel="noreferrer">Twitch Developer Console <ExternalLink size={12}/></a>
      </form>
      <form className="provider" onSubmit={event=>{
        event.preventDefault()
        perform("youtube",async()=>{
          await api("integrations/youtube","PUT",{mode,value:youtubeValue.trim()})
          setYouTubeValue("")
        },"YouTube credential verified and saved")
      }}>
        <header><span className="provider-icon red"><Youtube size={22}/></span><div><h3>YouTube</h3>
          <p className="secondary" role="status">{integration.youtubeAuth || (integration.youtubeConfigured?"Credential saved":"Connect to find YouTube livestreams")}</p></div></header>
        <label>Credential type<select value={mode} onChange={e=>{setMode(e.target.value);setYouTubeValue("")}}>
          <option value="api-key">YouTube Data API key</option><option value="access-token">OAuth access token</option></select></label>
        <label>{mode==="api-key"?"YouTube API key":"YouTube OAuth access token"}
          <input type="password" value={youtubeValue} onChange={e=>setYouTubeValue(e.target.value)} autoComplete="new-password" required minLength={10}
           placeholder={mode==="api-key"?"Paste your API key":"Paste your access token"}/></label>
        <p className="secondary">{mode==="api-key"?"Enable YouTube Data API v3 in your Google Cloud project.":"Use a token with youtube.readonly access. Replace it in Settings when it expires."}</p>
        <div className="toolbar"><Button variant="primary" disabled={Boolean(busy)||youtubeValue.trim().length<10}>{busy==="youtube"?"Checking…":"Verify and save"}</Button>
          {integration.youtubeConfigured&&<Button type="button" variant="ghost" disabled={Boolean(busy)} onClick={()=>
            perform("youtube",()=>api("integrations/youtube/test","POST"),"YouTube credential verified")}>Test</Button>}
        </div>
        <a className="provider-help" href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noreferrer">Google Cloud credentials <ExternalLink size={12}/></a>
      </form>
    </div>
  </section>
}

function SettingsView({state,task}: {state:State;task:Task}) {
  const [value,set]=useState(state.settings)
  const change=<K extends keyof Settings>(name:K,v:Settings[K])=>set(old=>({...old,[name]:v}))
  return <div className="settings-layout">
    <Connections integration={state.integration} task={task}/>
    <section className="panel"><h2>Recording and detection</h2><p className="secondary">Settings apply when a recording starts. The spike score combines unusual volume, relative increase, and how many people joined in.</p>
      <div className="settings-fields">
        <label>Stream quality<select value={value.quality} onChange={e=>change("quality",e.target.value)}>
          {["best","1080p","720p","480p","360p","worst"].map(q=><option key={q}>{q}</option>)}</select></label>
        <label>Detection sensitivity<select value={value.preset} onChange={e=>change("preset",e.target.value)}>
          {["Low","Balanced","High","Manual"].map(p=><option key={p}>{p}</option>)}</select></label>
        {fields.map(([key,label,detail])=><label key={key}>{label}<input type="number" step={["rise","deviations","disk_gb"].includes(key)?".1":"1"}
          value={String(value[key])} onChange={e=>change(key,+e.target.value)}/><small>{detail}</small></label>)}
        <label className="span-fields">Clip output directory<input value={value.output} onChange={e=>change("output",e.target.value)} placeholder="Default app data folder"/></label>
        <label className="check"><input type="checkbox" checked={value.nvenc} onChange={e=>change("nvenc",e.target.checked)}/> Use NVIDIA NVENC for clips</label>
        <label className="check"><input type="checkbox" checked={value.notification} onChange={e=>change("notification",e.target.checked)}/> Desktop notifications</label>
      </div><Button variant="primary" onClick={()=>task(()=>api("settings","PUT",value),"Settings saved")}>Save settings</Button>
    </section>
  </div>
}

function AnalyticsView({state}: {state:State}) {
  const [days,setDays]=useState("7")
  const [platform,setPlatform]=useState("")
  const [channel,setChannel]=useState("")
  const [simulated,setSimulated]=useState(false)
  const [data,setData]=useState<Analytics>()
  const [error,setError]=useState("")
  useEffect(()=>{
    let mounted=true
    const load=()=>api<Analytics>("analytics?"+new URLSearchParams({days,platform,channel,simulated:String(simulated)})).then(result=>{
      if(mounted){setData(result);setError("")}
    }).catch(e=>{if(mounted)setError(e.message)})
    void load(); const id=window.setInterval(load,15000)
    return ()=>{mounted=false;clearInterval(id)}
  },[days,platform,channel,simulated])
  const stats=data?.stats
  return <><div className="toolbar"><select aria-label="Time range" value={days} onChange={e=>setDays(e.target.value)}>
    <option value="1">Last 24 hours</option><option value="7">Last 7 days</option><option value="30">Last 30 days</option></select>
    <select aria-label="Analytics platform" value={platform} onChange={e=>setPlatform(e.target.value)}>
      <option value="">All platforms</option><option>Twitch</option><option>YouTube</option></select>
    <select aria-label="Analytics channel" value={channel} onChange={e=>setChannel(e.target.value)}><option value="">All streamers</option>
      {Array.from(new Set(state.channels.map(c=>c.name).concat(state.clips.map(c=>c.channel)))).map(c=><option key={c}>{c}</option>)}</select>
    <label className="check"><input type="checkbox" checked={simulated} onChange={e=>{setSimulated(e.target.checked);setChannel("");setPlatform("")}}/> Simulation history</label></div>
    {error&&<p className="error">{error}</p>}
    <div className="metrics">
      <Metric icon={<Clock3/>} label="Monitored hours" value={number.format(stats?.hours??0)}/>
      <Metric icon={<Zap/>} label="Detected events" value={stats?.events??0}/>
      <Metric icon={<Clapperboard/>} label="Saved clips" value={stats?.clips??0} note={(stats?.failed??0)+" failed clips"}/>
      <Metric icon={<HardDrive/>} label="Storage" value={size(stats?.bytes??0)}/>
    </div><div className="two-panels"><section className="panel"><h2>Strongest chat spikes</h2>
      {data?.peaks.length?data.peaks.map(p=><div className="activity-row" key={p.id}><Zap size={14}/>
        <div className="grow"><b>{p.channel}</b><small>{date(p.onset)} · {p.status}</small></div><b>{scoreLabel(p.score,p.scoreVersion)}</b></div>)
        :<p className="secondary">No detected events in this period.</p>}
    </section><section className="panel"><h2>Clips by streamer</h2>
      {data?.groups.length?data.groups.map(p=><div className="activity-row" key={p.channel}><Video size={14}/><div className="grow">{p.channel}</div>
        <b>{p.clips}</b><small>{size(p.bytes)}</small></div>):<p className="secondary">No saved clips in this period.</p>}
      <div className="detailed">
        <div>Average spike score <b>{stats?.score==null?"—":number.format(stats.score)+"/100"}</b></div>
        <div>Average clip duration <b>{stats?.duration==null?"—":number.format(stats.duration)+"s"}</b></div>
      </div>
    </section></div>
  </>
}

function ExtensionView({task}: {task:Task}) {
  const [code,setCode]=useState("")
  const [paired,setPaired]=useState(false)
  useEffect(()=>{
    let mounted=true
    const check=()=>void api<{paired:boolean}>("pair/status").then(value=>{
      if(mounted){setPaired(value.paired)}
    }).catch(()=>{})
    check()
    const timer=window.setInterval(check,2000)
    return ()=>{mounted=false;clearInterval(timer)}
  },[])
  return <section className="panel extension-panel">
    <header className="connection-row"><span className="provider-icon blue"><Puzzle size={22}/></span><div className="grow">
      <h2>Chrome companion</h2><p className="secondary">Save streamers and clip recent footage from the current tab.</p>
    </div><Pill tone={paired?"green":"muted"}>{paired?"Paired":"Unpaired"}</Pill></header>
    <ol><li>Open <code>chrome://extensions</code> and enable Developer mode.</li>
      <li>Choose <b>Load unpacked</b> and select <code>/opt/blynx-clips/extension</code> or the release’s <code>extension</code> folder.</li>
      <li>Generate a code, open the Blynx Clips Chrome popup, and paste the code.</li></ol>
    <div className="toolbar">
      <Button variant="primary" onClick={()=>task(async()=>{
        const result=await api<{code:string}>("pair","POST")
        setCode(result.code.slice(0,5)+"-"+result.code.slice(5))
      })}><Link2 size={15}/>Generate pairing code</Button>
      {code&&<Button onClick={()=>task(()=>navigator.clipboard.writeText(code),"Pairing code copied")}>Copy code</Button>}
      {paired&&<Button variant="ghost" onClick={()=>task(async()=>{await api("pair","DELETE");setPaired(false);setCode("")},"Extension unpaired")}>Unpair</Button>}
    </div>
    {code&&<div className="pair-code"><input className="token" aria-label="Pairing code" readOnly value={code}/><small>One use · expires after 5 minutes</small></div>}
  </section>
}

function App() {
  const [state,setState]=useState<State>()
  const [connected,setConnected]=useState(false)
  const [page,setPage]=useState<Page>(()=>new URLSearchParams(location.search).get("view")==="highlights"?"Highlights":"Dashboard")
  const [add,setAdd]=useState(false)
  const [clipId,openClip]=useState<string>()
  const [toast,setToast]=useState<{text:string;error:boolean}>()
  const refresh=()=>{void api<State>("state").then(setState).catch(()=>{})}
  useEffect(()=>{
    let ws:WebSocket|undefined, timer:number|undefined, disposed=false
    const connect=()=>{
      const url=new URL("/ws",location.href);url.protocol=location.protocol==="https:"?"wss:":"ws:"
      ws=new WebSocket(url)
      ws.onmessage=(event:MessageEvent<string>)=>{if(!disposed){setState(JSON.parse(event.data) as State);setConnected(true)}}
      ws.onerror=()=>ws?.close()
      ws.onclose=()=>{if(!disposed){setConnected(false);timer=window.setTimeout(connect,1800)}}
    }
    refresh();connect()
    return ()=>{disposed=true;clearTimeout(timer);ws?.close()}
  },[])
  useEffect(()=>{if(toast){const timer=setTimeout(()=>setToast(undefined),5500);return ()=>clearTimeout(timer)}},[toast])
  const task:Task=(operation,done)=>{void operation().then(()=>{if(done)setToast({text:done,error:false});refresh()}).catch(error=>setToast({text:error.message,error:true}))}
  const selection=state?.clips.find(c=>c.id===clipId)
  const active=state?.channels.filter(isActive)??[]
  const queued=state?.channels.filter(c=>c.recording==="Queued")??[]
  const waiting=state?.channels.filter(c=>!isActive(c) && c.recording!=="Queued" && c.enabled)??[]
  const clips=state?.clips??[]
  const firstRun=state && !state.integration.twitchConfigured && !state.integration.youtubeConfigured && !state.channels.length
  const captions:Record<Page,string>={
    "Dashboard":"", "Streamers":"Saved channels, capture priorities, and timing offsets.",
    "Live Monitoring":"Inspect live chat, detection, and video buffers.", "Highlights":"Review, trim, and export captured clips.",
    "Cut Clips":"Download your finished edits.",
    "Analytics":"Monitoring sessions and clip history.", "Chrome Extension":"Add the current stream from Chrome.",
    "Settings":"Platform connections, capture preferences, and detection thresholds.",
  }
  return <div className="app-shell">
    <aside className="sidebar"><button className="brand" onClick={()=>setPage("Dashboard")} aria-label="Blynx Clips dashboard">
      <span className="brand-icon"><Clapperboard size={23}/></span><span>Blynx <strong>Clips</strong></span>
    </button><nav aria-label="Main navigation">{pages.map(({name,icon:Icon})=><button key={name} title={name}
      className={name===page?"nav-item selected":"nav-item"} aria-current={name===page?"page":undefined} onClick={()=>setPage(name)}>
      <Icon size={17}/><span>{name}</span>{name===page&&<i/>}</button>)}</nav>
      <div className="sidebar-bottom"><Wifi size={16}/><div><b>{connected?"Service connected":"Connecting"}</b></div></div>
    </aside>
    <main><div className="topbar"><span className="breadcrumb"><Monitor size={15}/><span>{page}</span></span>
       <div className="top-actions">{state?.demo&&<Pill tone="amber">Simulation running</Pill>}
       {state&&<Button onClick={()=>task(()=>api("refresh","POST"),"Refresh requested")} title="Refresh and retry" aria-label="Refresh and retry"><RefreshCw size={15}/></Button>}
       <Button variant="primary" onClick={()=>setAdd(true)} disabled={!state}><Plus size={17}/><span>Add streamer</span></Button></div>
    </div>
    <div className="content"><header className="page-head"><div><h1>{page==="Dashboard"?"Overview":page}</h1>{captions[page]&&<p>{captions[page]}</p>}</div>
      {state&&(page==="Dashboard"||page==="Live Monitoring")&&<Button onClick={()=>task(()=>api("monitor/"+(state.running?"stop":"start"),"POST"),
        state.running?"Stopping all monitoring":"Monitoring enabled")} variant={state.running?"danger":"outline"}>{state.running?<Square size={14}/>:<Play size={15}/>}
        {state.running?"Stop all monitoring":"Start monitoring"}</Button>}</header>
    {!state?<div className="skeletons">{[1,2,3].map(n=><div key={n} className="skeleton"/>)}</div>:
      <motion.div key={page} initial={false} animate={{opacity:1}}>
        {!connected&&<div className="notice">Realtime connection interrupted. Reconnecting…</div>}
        {(page==="Dashboard"||page==="Live Monitoring")&&<>
          {page==="Dashboard"&&firstRun&&<section className="setup-banner">
            <div className="setup-copy">
              <h2>Connect a streaming platform</h2>
              <p className="secondary">Add Twitch or YouTube credentials to find live streams and save highlights when chat spikes.</p>
              <Button variant="primary" onClick={()=>setPage("Settings")}>Set up connections <ArrowRight size={15}/></Button>
            </div>
            <div className="setup-platforms" aria-hidden="true">
              <span className="setup-platform purple"><Twitch size={29}/></span>
              <span className="setup-platform red"><Youtube size={33}/></span>
            </div>
          </section>}
          {page==="Dashboard"&&!firstRun&&<div className="metrics overview-metrics">
            <Metric icon={<Radio/>} label="Recording slots" value={active.length+" / "+state.settings.slots} note={queued.length?queued.length+" queued":"Available capacity"}/>
            <Metric icon={<Zap/>} label="Chat messages / s" value={number.format(active.reduce((n,c)=>n+(c.mps??0),0))} note="Across active streams"/>
            <Metric icon={<Clapperboard/>} label="Saved highlights" value={clips.filter(c=>c.status==="ready"&&!c.simulated).length} note="Live platform clips"/>
            <Metric icon={<HardDrive/>} label="Tracked storage" value={size(state.system.bytes)} note={"Limit "+state.settings.disk_gb+" GB"}/>
          </div>}
          <div className="section-head"><h2>Active recordings <span>{active.length}</span></h2><div className="toolbar">
            <Button disabled={state.demo || active.length>=state.settings.slots} onClick={()=>task(()=>api("simulation","POST"),"Simulation started")}>
              <FlaskConical size={15}/>Run simulation</Button></div></div>
          {state.system.diskOk===false&&<div className="notice">Recording paused by the storage limit or available disk space.</div>}
          {active.length?<div className="stream-grid">{active.map(c=><StreamCard channel={c} clips={clips} key={c.id} detailed={page==="Live Monitoring"} onEvent={openClip} task={task}/>)}</div>:
            <Empty icon={<Radio/>} title="No active recordings"><p>{firstRun?"Run a simulation to try recording and clipping.":"Saved streamers will appear here when recording begins."}</p></Empty>}
          {queued.length>0&&<section className="panel queue"><h2>Waiting queue</h2>{queued.map(c=><ChannelRow channel={c} key={c.id} task={task}/>)}</section>}
          {page==="Live Monitoring"&&waiting.length>0&&<section className="panel queue"><h2>Waiting for broadcasts</h2>
            {waiting.map(c=><ChannelRow channel={c} key={c.id} task={task}/>)}</section>}
          <div className="two-panels"><section className="panel"><div className="section-head"><h2>Activity</h2><Bell size={16}/></div>
            {state.activities.length?state.activities.slice(0,8).map((item,i)=><div className="activity-row" key={item.at+"-"+i}>
              <span className={"log-dot "+item.level}/><span className="grow">{item.message}</span><small>{time(item.at)}</small></div>):
              <p className="secondary">No recent monitoring activity.</p>}</section>
            <section className="panel"><div className="section-head"><h2>Local service</h2><Cpu size={17}/></div>
            <div className="detailed"><div>Backend CPU <b>{state.system.cpu}%</b></div><div>Process memory <b>{size(state.system.memory)}</b></div>
              <div>Child processes <b>{state.system.processes}</b></div><div>Tracked storage <b>{size(state.system.bytes)}</b></div></div>
              <Button variant="ghost" onClick={()=>setPage("Settings")}>Recording settings <ArrowRight size={14}/></Button></section></div>
        </>}
        {page==="Streamers"&&(state.channels.filter(c=>c.platform!=="Simulation").length?<section className="panel">
          {state.channels.filter(c=>c.platform!=="Simulation").map(c=><ChannelRow key={c.id} channel={c} task={task}/>)}
        </section>:<Empty icon={<Users/>} title="No streamers saved"><p>Add a Twitch or YouTube channel URL.</p>
          <Button variant="primary" onClick={()=>setAdd(true)}><Plus size={16}/>Add streamer</Button></Empty>)}
        {page==="Highlights"&&<ClipLibrary clips={clips} open={openClip}/>}
        {page==="Cut Clips"&&<CutClips openHighlights={()=>setPage("Highlights")}/>}
        {page==="Analytics"&&<AnalyticsView state={state}/>}
        {page==="Chrome Extension"&&<ExtensionView task={task}/>}
        {page==="Settings"&&<SettingsView key={JSON.stringify(state.settings)} state={state} task={task}/>}
      </motion.div>}
    </div></main>
    <AnimatePresence>{add&&<AddChannel close={()=>setAdd(false)} refresh={refresh} setup={()=>{setAdd(false);setPage("Settings")}}/>}
      {selection&&<ClipDetail key={selection.id} clip={selection} close={()=>openClip(undefined)} task={task}
        openCuts={()=>{openClip(undefined);setPage("Cut Clips")}}/>}
      {toast&&<motion.div role="status" className={toast.error?"toast error":"toast"} initial={{opacity:0,y:12}}
        animate={{opacity:1,y:0}} exit={{opacity:0}}>{toast.error?<CircleHelp size={16}/>:<Activity size={16}/>} {toast.text}
        <Button variant="ghost" aria-label="Dismiss" onClick={()=>setToast(undefined)}><X size={15}/></Button></motion.div>}</AnimatePresence>
  </div>
}

createRoot(document.getElementById("root")!).render(<App/>)

