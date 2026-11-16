const limit = 46 * 1024 * 1024
let state = "idle"
let result = ""
let recorder

function badge(recording) {
  return chrome.runtime.sendMessage({target: "worker", type: "badge", recording}).catch(() => {})
}

function snapshot() {
  return {state, result}
}

async function start({url, streamId, token}) {
  if (["starting", "recording", "saving"].includes(state)) throw new Error("A tab recording is already active.")
  state = "starting"
  result = ""
  let stream
  let audio
  let timer
  const release = () => {
    clearTimeout(timer)
    stream?.getTracks().forEach(track => track.stop())
    if (audio) void audio.close().catch(() => {})
    void badge(false)
  }
  try {
    if (!token) throw new Error("Pair the extension first.")
    const tab = {mandatory: {chromeMediaSource: "tab", chromeMediaSourceId: streamId}}
    stream = await navigator.mediaDevices.getUserMedia({audio: tab, video: tab})
    if (stream.getAudioTracks().length) {
      audio = new AudioContext()
      audio.createMediaStreamSource(stream).connect(audio.destination)
      void audio.resume().catch(() => {})
    }
    const mimeType = ["video/webm;codecs=vp8,opus", "video/webm;codecs=vp9,opus", "video/webm"]
      .find(type => MediaRecorder.isTypeSupported(type))
    if (!mimeType) throw new Error("Chrome cannot encode WebM tab video.")
    recorder = new MediaRecorder(stream, {mimeType, videoBitsPerSecond: 4000000})
    const chunks = []
    let bytes = 0
    const stop = () => { if (recorder?.state === "recording") recorder.stop() }
    let recorderError = ""
    recorder.ondataavailable = event => {
      if (event.data.size) {
        bytes += event.data.size
        chunks.push(event.data)
        if (bytes > limit) stop()
      }
    }
    recorder.onerror = () => {
      recorderError = "Chrome lost access to the tab recording."
      stop()
    }
    recorder.onstop = async () => {
      state = "saving"
      release()
      recorder = undefined
      try {
        if (recorderError) throw new Error(recorderError)
        const body = new Blob(chunks, {type: "video/webm"})
        const response = await fetch("http://127.0.0.1:17865/api/extension/tab-recordings?" + new URLSearchParams({url}), {
          method: "POST", headers: {Authorization: "Bearer " + token, "Content-Type": "video/webm"},
          body, signal: AbortSignal.timeout(165000)
        })
        const data = await response.json()
        if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "The app rejected the recording.")
        result = "Saved " + data.duration + " seconds to Highlights."
        state = "saved"
      } catch (error) {
        result = error instanceof TypeError ? "Blynx Clips disconnected before the recording could be saved." : error.message
        state = "failed"
      }
    }
    stream.getTracks().forEach(track => track.addEventListener("ended", stop))
    recorder.start(1000)
    timer = setTimeout(stop, 60000)
    state = "recording"
    void badge(true)
    return snapshot()
  } catch (error) {
    release()
    recorder = undefined
    state = "failed"
    result = error.message
    return snapshot()
  }
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (message.target !== "offscreen" || sender.id !== chrome.runtime.id) return
  if (message.type === "start") {
    start(message).then(reply, error => reply({error: error.message}))
    return true
  }
  if (message.type === "stop" && recorder?.state === "recording") {
    state = "saving"
    recorder.stop()
  }
  reply(snapshot())
})
