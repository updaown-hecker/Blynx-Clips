const origin = "http://127.0.0.1:17865"
const elements = Object.fromEntries(Array.from(document.querySelectorAll("[id]"), item => [item.id, item]))
const streamHosts = new Set([
  "twitch.tv", "www.twitch.tv", "m.twitch.tv", "youtube.com", "www.youtube.com",
  "m.youtube.com", "gaming.youtube.com", "youtu.be"
])
let url = ""
let token = ""
let busy = false
let paired = false
let tabId
let captureState = "idle"

async function request(path, method = "GET", data, credential = token) {
  let response
  try {
    response = await fetch(origin + "/api/extension/" + path, {
      method,
      headers: {
        ...(credential && {Authorization: "Bearer " + credential}),
        ...(data && {"Content-Type": "application/json"})
      },
      body: data ? JSON.stringify(data) : undefined,
      signal: AbortSignal.timeout(15000)
    })
  } catch {
    throw new Error("Open Blynx Clips to start the local service.")
  }
  const result = await response.json()
  if (!response.ok) {
    const message = typeof result.detail === "string" ? result.detail : "Check the information you entered."
    throw Object.assign(new Error(message), {status: response.status})
  }
  return result
}

function showPairing(message = "Generate a pairing code in the dashboard’s Chrome Extension page.") {
  paired = false
  elements.service.textContent = message
  elements.pair.hidden = false
  elements.actions.hidden = true
  elements.forget.hidden = !token
  elements.stream.hidden = true
}

async function refresh(message) {
  if (!token) return showPairing(message)
  try {
    const data = await request("status?" + new URLSearchParams({url}))
    paired = true
    elements.service.textContent = message ||
      (data.running ? "Connected · " + data.active + " active" : "Connected · Monitoring paused")
    elements.pair.hidden = true
    elements.forget.hidden = false
    elements.stream.hidden = false
    elements.actions.hidden = false
    elements.add.hidden = !url
    elements.current.textContent = data.channel?.name || (url ? new URL(url).hostname : "No stream tab")
    elements.recording.textContent = data.channel?.recording || (url ? "Ready to add" : "Open a Twitch or YouTube stream.")
    elements.add.hidden = !url || Boolean(data.channel)
    elements.clip.hidden = !data.channel
    elements.clip.disabled = !data.channel?.canClip
    await updateCapture()
  } catch (error) {
    if (error.status === 401) {
      token = ""
      await chrome.storage.local.remove("token")
      showPairing(error.message)
    } else {
      elements.actions.hidden = true
      elements.service.textContent = error.message
    }
  }
}

async function action(operation) {
  if (busy) return
  busy = true
  try {
    await operation()
  } catch (error) {
    elements.service.textContent = error.message
  } finally {
    busy = false
  }
}

elements.pair.addEventListener("submit", event => {
  event.preventDefault()
  void action(async () => {
    const result = await request("pair", "POST", {code: elements.code.value.trim()}, "")
    await chrome.storage.local.set({token: result.token})
    token = result.token
    elements.code.value = ""
    await refresh("Extension paired")
  })
})

elements.add.addEventListener("click", () => void action(async () => {
  const result = await request("channels", "POST", {url})
  await refresh(result.name + " added to Blynx Clips")
}))

elements.clip.addEventListener("click", () => void action(async () => {
  const result = await request("clips", "POST", {url})
  elements.highlights.hidden = false
  await refresh("Saving " + result.duration + " seconds to Highlights")
}))

function renderCapture(data) {
  if (data.error) throw new Error(data.error)
  captureState = data.state
  const recording = captureState === "recording"
  const pending = ["starting", "saving"].includes(captureState)
  elements.capture.hidden = !url && !recording && !pending
  elements.capture.disabled = pending
  elements.capture.textContent = recording ? "Stop and save tab recording" :
    (pending ? "Saving tab recording…" : "Record this tab · up to 60 seconds")
  if (recording) elements.service.textContent = "Recording this tab"
  if (data.result) elements.service.textContent = data.result
  if (captureState === "saved") elements.highlights.hidden = false
}

function capture(type) {
  return chrome.runtime.sendMessage({target: "worker", type, url, tabId})
}

async function updateCapture() {
  renderCapture(await capture("status"))
}

elements.capture.addEventListener("click", () => void action(async () => {
  renderCapture(await capture(captureState === "recording" ? "stop" : "start"))
}))

elements.forget.addEventListener("click", () => void action(async () => {
  token = ""
  await chrome.storage.local.remove("token")
  showPairing()
}))

async function init() {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true})
  if (tab?.url) {
    const page = new URL(tab.url)
    if (page.protocol === "https:" && streamHosts.has(page.hostname) && page.pathname !== "/") {
      url = page.href
      tabId = tab.id
    }
  }
  token = (await chrome.storage.local.get("token")).token ?? ""
  await refresh()
  setInterval(() => { if (!busy && paired) void refresh() }, 3500)
}
void init().catch(error => { elements.service.textContent = error.message })
