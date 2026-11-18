import { useEffect, useRef, useState, type PointerEvent } from "react"
import { ArrowDownToLine, Captions, Clapperboard, Crop, Film, Pause, Pencil, Play, ScanFace, Scissors, X } from "lucide-react"
import { Button } from "./button"
import { api, type Clip, type CutClip } from "./types"
import "./cut-editor.css"

type Region = {x:number; y:number; width:number; height:number}
type Cue = {start:number; end:number; text:string}
type Analysis = {camera:Region|null; cues:Cue[]; camera_error:string; caption_error:string}
type Job<T> = {status:"queued"|"running"|"done"|"error"; message:string; result:T|null}
const layouts = [
  ["fill", "Fill frame"], ["fit", "Fit full video"], ["camera-top", "Camera on top"], ["camera-bottom", "Camera below"],
] as const
type Layout = typeof layouts[number][0]
const styles = [["clean","Clean"],["pop","Bold yellow"],["boxed","Boxed"],["off","Off"]] as const
type CaptionStyle = typeof styles[number][0]
type EditorSource = Pick<Clip,"id"|"name"|"channel"|"platform"|"duration"> & {kind:"clip"|"cut"}
type Recipe = {start:number; end:number; layout:Layout; camera:Region|null; caption_style:CaptionStyle; cues:Cue[]}
type SavedCutEdit = {source:EditorSource; recipe:Recipe|null}
const videoRegion: Region = {x:0,y:0,width:1,height:1}
const stamp = (seconds:number) => {
  const tenths = Math.round(Math.max(0, seconds) * 10)
  return Math.floor(tenths / 600) + ":" + String(Math.floor(tenths % 600 / 10)).padStart(2,"0") + "." + tenths % 10
}

async function watchJob<T>(job:string, signal:AbortSignal, update:(state:Job<T>)=>void): Promise<T> {
  while (!signal.aborted) {
    const state = await api<Job<T>>("editor-jobs/"+job, "GET", undefined, signal)
    if (signal.aborted) break
    update(state)
    if (state.status==="error") throw Error(state.message)
    if (state.status==="done" && state.result) return state.result
    await new Promise(resolve=>setTimeout(resolve,900))
  }
  throw new DOMException("Editor closed", "AbortError")
}

function drawVideo(canvas:HTMLCanvasElement|null, video:HTMLVideoElement, layout:Layout, camera:Region|null) {
  const ctx = canvas?.getContext("2d")
  if (!canvas || !ctx || !video.videoWidth || video.readyState < 2) return
  ctx.fillStyle="#08090c"
  ctx.fillRect(0,0,canvas.width,canvas.height)
  const draw = (region:Region, y:number, height:number, fit=false)=>{
    const width = canvas.width
    const sourceWidth = region.width*video.videoWidth
    const sourceHeight = region.height*video.videoHeight
    const scale = (fit?Math.min:Math.max)(width/sourceWidth,height/sourceHeight)
    const croppedWidth = Math.min(sourceWidth,width/scale)
    const croppedHeight = Math.min(sourceHeight,height/scale)
    const outputWidth = croppedWidth*scale
    const outputHeight = croppedHeight*scale
    ctx.drawImage(video,region.x*video.videoWidth+(sourceWidth-croppedWidth)/2,
      region.y*video.videoHeight+(sourceHeight-croppedHeight)/2,croppedWidth,croppedHeight,
      (width-outputWidth)/2,y+(height-outputHeight)/2,outputWidth,outputHeight)
  }
  if (camera && layout.startsWith("camera-")) {
    const top = layout==="camera-top"
    const faceHeight = canvas.height/3
    draw(camera,top?0:faceHeight*2,faceHeight)
    draw(videoRegion,top?faceHeight:0,faceHeight*2)
  } else draw(videoRegion,0,canvas.height,layout==="fit")
}

export function CutDownload({id}: {id:string}) {
  return <Button asChild><a href={`/media/cut-clips/${id}/download`} download>
    <ArrowDownToLine size={15}/>Download MP4
  </a></Button>
}

function ShortEditor({clip,recipe,onSaved}: {clip:EditorSource;recipe?:Recipe|null;onSaved:()=>void}) {
  const video = useRef<HTMLVideoElement>(null)
  const phone = useRef<HTMLCanvasElement>(null)
  const source = useRef<HTMLCanvasElement>(null)
  const exportController = useRef<AbortController>(null)
  const dragStart = useRef<{x:number;y:number}|null>(null)
  const manualCamera = useRef(Boolean(recipe?.camera))
  const maximum = Math.min(300, clip.duration)
  const endpoint = `${clip.kind==="cut"?"cut-clips":"clips"}/${clip.id}`
  const [start,setStart] = useState(recipe?.start??0)
  const [end,setEnd] = useState(recipe?.end??maximum)
  const [playhead,setPlayhead] = useState(recipe?.start??0)
  const [playing,setPlaying] = useState(false)
  const [sourceRatio,setSourceRatio] = useState(16/9)
  const [layout,setLayout] = useState<Layout>(recipe?.layout??(clip.kind==="cut"?"fit":"fill"))
  const [captionStyle,setCaptionStyle] = useState<CaptionStyle>(recipe?.caption_style??(clip.kind==="cut"?"off":"clean"))
  const [camera,setCamera] = useState<Region|null>(recipe?.camera??null)
  const [drawCrop,setDrawCrop] = useState(false)
  const [draft,setDraft] = useState<Region|null>(null)
  const [cues,setCues] = useState<Cue[]>(recipe?.cues??[])
  const [activity,setActivity] = useState("Preparing clip analysis")
  const [analysisDone,setAnalysisDone] = useState(Boolean(recipe))
  const [analysisError,setAnalysisError] = useState("")
  const [captionError,setCaptionError] = useState("")
  const [cameraError,setCameraError] = useState("")
  const [analysisAttempt,retryAnalysis] = useState(0)
  const [exportMessage,setExportMessage] = useState("")
  const [exportError,setExportError] = useState("")
  const [saved,setSaved] = useState("")
  const [previewError,setPreviewError] = useState("")

  useEffect(()=>{
    if (recipe && !analysisAttempt) return
    const controller = new AbortController()
    const {signal} = controller
    setAnalysisDone(false)
    setAnalysisError("")
    setActivity("Preparing clip analysis")
    const applyCamera = (result:Analysis)=>{
      if (!manualCamera.current && result.camera) setCamera(result.camera)
      setCameraError(result.camera_error)
    }
    void api<{job:string}>(endpoint+"/editor-analysis","POST",undefined,signal)
      .then(({job})=>watchJob<Analysis>(job,signal,state=>{
        setActivity(state.message)
        if (state.result) applyCamera(state.result)
      })).then(result=>{
        if (signal.aborted) return
        setCues(result.cues)
        setCaptionError(result.caption_error)
        setAnalysisDone(true)
      }).catch(error=>{
        if (!signal.aborted) {
          setAnalysisError(error.message)
          setAnalysisDone(true)
        }
      })
    return ()=>controller.abort()
  },[endpoint,recipe,analysisAttempt])

  useEffect(()=>()=>exportController.current?.abort(),[])

  useEffect(()=>{
    const media = video.current
    if (!media) return
    let frame = 0
    let lastUpdate = -Infinity
    const paint = ()=>{
      drawVideo(phone.current,media,layout,camera)
      drawVideo(source.current,media,"fit",null)
    }
    const tick = (now:number)=>{
      if (media.currentTime >= end) {
        media.pause()
        media.currentTime=end
      }
      if (now-lastUpdate>=80 || media.paused) {
        setPlayhead(media.currentTime)
        lastUpdate=now
      }
      paint()
      if (!media.paused) frame=requestAnimationFrame(tick)
    }
    const resume = ()=>{
      cancelAnimationFrame(frame)
      frame=requestAnimationFrame(tick)
    }
    paint()
    if (!media.paused) resume()
    media.addEventListener("play",resume)
    media.addEventListener("loadeddata",paint)
    media.addEventListener("seeked",paint)
    return ()=>{
      cancelAnimationFrame(frame)
      media.removeEventListener("play",resume)
      media.removeEventListener("loadeddata",paint)
      media.removeEventListener("seeked",paint)
    }
  },[layout,camera,end,drawCrop,sourceRatio])

  const seek = (position:number)=>{
    if (video.current) {
      video.current.currentTime=position
      setPlayhead(position)
    }
  }
  const togglePlayback = ()=>{
    const media = video.current
    if (!media) return
    if (!media.paused) {
      media.pause()
      return
    }
    if (media.currentTime < start || media.currentTime >= end-0.05) seek(start)
    void media.play().catch(()=>setPreviewError("Could not play this source video."))
  }

  const point = (event:PointerEvent<HTMLDivElement>)=>{
    const rect = event.currentTarget.getBoundingClientRect()
    return {x:Math.min(1,Math.max(0,(event.clientX-rect.left)/rect.width)),
      y:Math.min(1,Math.max(0,(event.clientY-rect.top)/rect.height))}
  }
  const selection = (event:PointerEvent<HTMLDivElement>)=>{
    const anchor = dragStart.current
    if (!anchor) return null
    const to = point(event)
    return {x:Math.min(anchor.x,to.x),y:Math.min(anchor.y,to.y),
      width:Math.abs(anchor.x-to.x),height:Math.abs(anchor.y-to.y)}
  }
  const chooseCamera = (event:PointerEvent<HTMLDivElement>)=>{
    const region = selection(event)
    dragStart.current=null
    setDraft(null)
    if (region && region.width>=0.04 && region.height>=0.04) {
      manualCamera.current=true
      setCamera(region)
    }
  }
  const activeCue = captionStyle!=="off" && cues.find(cue=>playhead>=cue.start && playhead<cue.end)
  const stack = layout.startsWith("camera-")
  const canRender = end-start>=1 && (!stack || camera) &&
    (captionStyle==="off" || (analysisDone && !analysisError && !captionError))
  const exportCut = async ()=>{
    const controller = new AbortController()
    exportController.current=controller
    const {signal} = controller
    setSaved("")
    setExportError("")
    setExportMessage("Sending edit to server")
    try {
      const {job} = await api<{job:string}>(endpoint+"/editor-export", "POST",
        {start,end,layout,camera,caption_style:captionStyle,cues},signal)
      const result = await watchJob<{id:string}>(job,signal,state=>setExportMessage(state.message))
      setSaved(result.id)
      onSaved()
    } catch (error) {
      if (!signal.aborted) setExportError((error as Error).message)
    } finally {
      if (!signal.aborted) setExportMessage("")
    }
  }
  const crop = draft ?? camera
  const timelineStep = 0.01

  return <div className="short-editor">
    <video ref={video} src={clip.kind==="cut"?`/media/cut-clips/${clip.id}/video`:`/media/${clip.id}/editor-source`}
      preload="auto" playsInline className="short-source"
      onLoadedMetadata={event=>{
        const media=event.currentTarget
        if (media.videoHeight) setSourceRatio(media.videoWidth/media.videoHeight)
        if (start>0) media.currentTime=start
      }}
      onPlay={()=>setPlaying(true)} onPause={()=>setPlaying(false)}
      onError={()=>setPreviewError("The browser could not load the source video.")}/>
    <section className="short-stage" aria-label="Video preview">
      <div className="short-preview-header"><span>Preview</span><span>9:16 · 1080 × 1920</span></div>
      <div className="short-phone" data-layout={layout}>
        <canvas ref={phone} width={360} height={640} aria-label="Vertical preview"/>
        {clip.platform==="Twitch"&&<div className="short-credit">Twitch: {clip.channel}</div>}
        {activeCue&&<div className={`short-subtitle short-subtitle-${captionStyle}`}><span>{activeCue.text}</span></div>}
      </div>
      {previewError&&<p className="error" role="alert">{previewError}</p>}
      <div className="short-transport">
        <Button onClick={togglePlayback} aria-label={playing?"Pause preview":"Play preview"}>{playing?<Pause size={17}/>:<Play size={17}/>}</Button>
        <span>{stamp(playhead)} <span className="secondary">/ {stamp(maximum)}</span></span>
      </div>
    </section>
    <section className="short-inspector" aria-label="Edit settings">
      <div className="short-pane">
        <h3><Film size={16}/>Layout</h3>
        <div className="short-choices">{layouts.map(([value,label])=>
          <Button key={value} aria-pressed={layout===value} onClick={()=>{
            setLayout(value)
            if (value.startsWith("camera-") && !camera) {
              video.current?.pause()
              setDrawCrop(true)
            }
          }}>{label}</Button>)}</div>
        <div className="short-camera-header">
          <span className="secondary"><ScanFace size={14}/>{camera?(manualCamera.current?"Manual camera crop":"Camera crop detected"):
            (cameraError || (analysisDone?"No camera detected":"Camera scan pending"))}</span>
          <Button variant="ghost" onClick={()=>{video.current?.pause();setDrawCrop(value=>!value)}}>
            <Crop size={14}/>{drawCrop?"Close crop":camera?"Adjust crop":"Draw crop"}</Button>
        </div>
        {drawCrop&&<>
          <p className="secondary">Drag around the webcam in the source frame.</p>
          <div className="short-crop" style={{aspectRatio:sourceRatio}}
            onPointerDown={event=>{
              if (event.button!==0) return
              event.currentTarget.setPointerCapture(event.pointerId)
              dragStart.current=point(event)
            }}
            onPointerMove={event=>{if (dragStart.current) setDraft(selection(event))}}
            onPointerUp={chooseCamera}
            onPointerCancel={()=>{dragStart.current=null;setDraft(null)}}>
            <canvas ref={source} width={480} height={Math.max(1,Math.round(480/sourceRatio))}/>
            {crop&&<div className="short-crop-box" style={{left:crop.x*100+"%",top:crop.y*100+"%",
              width:crop.width*100+"%",height:crop.height*100+"%"}}/>}
          </div>
          <label className="short-field">Camera crop anchor
            <select value="" onChange={event=>{
              const [x=0,y=0]=event.target.value.split(",").map(Number)
              manualCamera.current=true
              setCamera({x,y,width:0.28,height:0.32})
            }}>
              <option value="" disabled>Pick a corner</option>
              <option value="0,0">Top left</option><option value="0.72,0">Top right</option>
              <option value="0,0.68">Bottom left</option><option value="0.72,0.68">Bottom right</option>
            </select>
          </label>
        </>}
      </div>
      <div className="short-pane">
        <h3><Captions size={16}/>Captions</h3>
        <div className="short-choices">{styles.map(([value,label])=>
          <Button key={value} aria-pressed={captionStyle===value} onClick={()=>setCaptionStyle(value)}>{label}</Button>)}</div>
        {!analysisDone&&<p className="secondary" role="status">{activity}…</p>}
        {(analysisError||captionError)&&<div className="short-camera-header">
          <p className="error" role="alert">{analysisError||captionError}</p>
          <Button onClick={()=>retryAnalysis(value=>value+1)}>Retry</Button>
        </div>}
        {analysisDone&&!analysisError&&!captionError&&!cues.length&&
          (recipe&&!analysisAttempt
            ? <Button onClick={()=>retryAnalysis(value=>value+1)}>Generate captions</Button>
            : <p className="secondary">No speech was detected.</p>)}
        {cues.length>0&&captionStyle!=="off"&&<details className="short-transcript">
          <summary>Edit caption text · {cues.length} cues</summary>
          <div>{cues.map((cue,index)=><label key={index} className="short-cue">
            <Button onClick={()=>seek(cue.start)} aria-label={"Seek to "+stamp(cue.start)}>{stamp(cue.start)}</Button>
            <input aria-label={"Caption "+(index+1)} value={cue.text} maxLength={180} onChange={event=>{
              const text=event.target.value
              setCues(previous=>previous.map((item,i)=>i===index?{...item,text}:item))
            }}/>
          </label>)}</div>
        </details>}
      </div>
      <div className="short-pane short-export">
        <Button variant="primary" onClick={()=>void exportCut()} disabled={!canRender||Boolean(exportMessage)}>
          <Scissors size={16}/>{exportMessage||(recipe!==undefined?"Save new version":"Export short")}
        </Button>
        {saved&&<CutDownload id={saved}/>}
        {exportError&&<p className="error" role="alert">{exportError}</p>}
        {saved&&<p className="secondary" role="status">Saved under Finished cuts.</p>}
      </div>
    </section>
    <section className="short-timeline" aria-label="Trim timeline">
      <div className="short-timeline-heading"><h3>Timeline</h3><span>{stamp(end-start)} selected</span></div>
      <label className="short-track">
        <span>Playhead</span>
        <div className="short-scrubber">
          <div className="short-trim-range" style={{left:start/maximum*100+"%",width:(end-start)/maximum*100+"%"}}/>
          <input type="range" min={0} max={maximum} step={timelineStep} value={Math.min(maximum,playhead)}
            onChange={event=>{video.current?.pause();seek(Number(event.target.value))}} aria-label="Seek preview"/>
        </div>
        <span>{stamp(playhead)}</span>
      </label>
      <label className="short-track"><span>In</span>
        <input type="range" min={0} max={Math.max(0,end-1)} step={timelineStep} value={start}
          onChange={event=>{video.current?.pause();const next=Number(event.target.value);setStart(next);seek(next)}}/>
        <span>{stamp(start)}</span>
      </label>
      <label className="short-track"><span>Out</span>
        <input type="range" min={start+1} max={maximum} step={timelineStep} value={end}
          onChange={event=>{video.current?.pause();const next=Number(event.target.value);setEnd(next);seek(next)}}/>
        <span>{stamp(end)}</span>
      </label>
    </section>
  </div>
}

export function CutClips({clips,selected,onSelect,openHighlights}: {
  clips:Clip[];selected:string;onSelect:(id:string)=>void;openHighlights:()=>void
}) {
  const [cuts,setCuts]=useState<CutClip[]>()
  const [error,setError]=useState("")
  const [attempt,reload]=useState(0)
  const [playing,setPlaying]=useState("")
  const [editCut,setEditCut]=useState<CutClip>()
  const [edit,setEdit]=useState<SavedCutEdit>()
  const [editError,setEditError]=useState("")
  const editorTop=useRef<HTMLDivElement>(null)
  const ready=clips.filter(clip=>clip.status==="ready"&&clip.duration>=1)
  const clip=ready.find(item=>item.id===selected)
  useEffect(()=>{
    const controller=new AbortController()
    const {signal}=controller
    setError("")
    void api<CutClip[]>("cut-clips","GET",undefined,signal).then(result=>{
      if(!signal.aborted) setCuts(result)
    }).catch(error=>{
      if(!signal.aborted) setError(error.message)
    })
    return ()=>controller.abort()
  },[attempt])
  useEffect(()=>{
    if (!editCut) return
    const controller=new AbortController()
    const {signal}=controller
    void api<SavedCutEdit>(`cut-clips/${editCut.id}/edit`,"GET",undefined,signal).then(result=>{
      if (!signal.aborted) setEdit(result)
    }).catch(error=>{
      if (!signal.aborted) setEditError(error.message)
    })
    return ()=>controller.abort()
  },[editCut])

  const reopen=(cut:CutClip)=>{
    if (cut.id!==editCut?.id || editError) {
      setEdit(undefined)
      setEditError("")
      setEditCut({...cut})
      setPlaying("")
    }
    editorTop.current?.focus({preventScroll:true})
    editorTop.current?.scrollIntoView({block:"start"})
  }
  const leaveCut=()=>{
    setEditCut(undefined)
    setEdit(undefined)
    setEditError("")
  }
  return <div className="cut-workspace">
    <div className="short-source-picker" ref={editorTop} tabIndex={-1}>
      <label className="short-field" htmlFor="short-source">Source highlight</label>
      <select id="short-source" value={editCut?"":clip?.id??""} onChange={event=>{
        leaveCut()
        onSelect(event.target.value)
      }}>
        <option value="">{ready.length?"Choose a highlight":"No completed highlights"}</option>
        {ready.map(item=><option key={item.id} value={item.id}>{item.name} · {item.channel}</option>)}
      </select>
      <Button variant="ghost" onClick={openHighlights}>Browse Highlights</Button>
    </div>
    {editCut ? <>
      <div className="section-head"><h2>Editing {editCut.name}</h2>
        <Button variant="ghost" onClick={leaveCut}>Close edit</Button></div>
      {editError&&<p className="error" role="alert">{editError}</p>}
      {!edit&&!editError&&<p className="secondary" role="status">Opening saved cut…</p>}
      {edit&&<>
        {edit.source.kind==="cut"&&<p className="secondary">
          This edit uses the exported video. Its existing captions and framing are baked in.
        </p>}
        <ShortEditor key={"edit-"+editCut.id} clip={edit.source} recipe={edit.recipe}
          onSaved={()=>reload(value=>value+1)}/>
      </>}
    </>:clip?<ShortEditor key={clip.id} clip={{...clip,kind:"clip"}} onSaved={()=>reload(value=>value+1)}/>:
      <div className="empty"><div className="empty-icon"><Clapperboard/></div>
        <h3>Choose a highlight to edit</h3><p>Trim it, generate captions, and arrange it for a phone screen.</p></div>}
    <section className="panel finished-cuts" aria-label="Finished cuts">
      <div className="section-head"><h2>Finished cuts</h2><Button onClick={()=>reload(value=>value+1)}>Refresh</Button></div>
      {error&&<p className="error" role="alert">{error}</p>}
      {!error&&!cuts&&<p role="status" className="secondary">Loading cut clips…</p>}
      {cuts?.length===0&&<p className="secondary">Your exported videos will appear here.</p>}
      {cuts?.map(cut=><div key={cut.id} className="finished-cut">
        <div className="channel-row">
          <span className="avatar"><Scissors size={17}/></span>
          <div className="grow"><div className="truncate" title={cut.name}><strong>{cut.name}</strong></div>
            <p className="secondary">{[cut.channel,cut.preset==="vertical"?"9:16":"Original aspect",
              cut.duration===null?"":stamp(cut.duration),(cut.bytes/1e6).toFixed(1)+" MB",
              new Date(cut.created*1000).toLocaleString([], {dateStyle:"medium",timeStyle:"short"})].filter(Boolean).join(" · ")}</p>
          </div>
          <Button onClick={()=>setPlaying(playing===cut.id?"":cut.id)}>
            {playing===cut.id?<X size={15}/>:<Play size={15}/>} {playing===cut.id?"Close":"Preview"}
          </Button>
          <Button onClick={()=>reopen(cut)}><Pencil size={15}/>Edit</Button>
          <CutDownload id={cut.id}/>
        </div>
        {playing===cut.id&&<video className="finished-preview" controls playsInline src={`/media/cut-clips/${cut.id}/video`}/>}
      </div>)}
    </section>
  </div>
}
