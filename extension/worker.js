const offscreenURL = chrome.runtime.getURL("offscreen.html")

async function sendCapture(message) {
  const contexts = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"], documentUrls: [offscreenURL]
  })
  if (!contexts.length) {
    if (message.type !== "start") return {state: "idle"}
    await chrome.offscreen.createDocument({
      url: "offscreen.html", reasons: ["USER_MEDIA"],
      justification: "Record audio and video from the stream tab the user selected."
    })
  }
  if (message.type === "start") {
    const {token} = await chrome.storage.local.get("token")
    if (!token) throw new Error("Pair the extension first.")
    const streamId = await chrome.tabCapture.getMediaStreamId({targetTabId: message.tabId})
    return chrome.runtime.sendMessage({target: "offscreen", ...message, streamId, token})
  }
  return chrome.runtime.sendMessage({target: "offscreen", ...message})
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (message.target !== "worker") return
  if (sender.url === offscreenURL && message.type === "badge") {
    void chrome.action.setBadgeBackgroundColor({color: "#c41f39"})
    void chrome.action.setBadgeText({text: message.recording ? "REC" : ""})
    reply({ok: true})
    return
  }
  if (sender.url !== chrome.runtime.getURL("popup.html")) return
  const {type, tabId, url} = message
  sendCapture({type, tabId, url}).then(reply, error => reply({error: error.message}))
  return true
})
