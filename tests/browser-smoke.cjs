const {chromium} = require("playwright")
const {spawn} = require("node:child_process")
const assert = require("node:assert/strict")
const os = require("node:os")
const path = require("node:path")
const {mkdtemp, rm} = require("node:fs/promises")

async function main() {
  const data = await mkdtemp(path.join(os.tmpdir(), "blynx-browser-"))
  const port = "17865"
  const origin = "http://127.0.0.1:" + port
  const extension = path.resolve("extension")
  const app = spawn(process.env.BLYNX_PYTHON || "python3", ["-m","blynx"], {
    env: {...process.env, PYTHONPATH:path.resolve("backend"), BLYNX_DATA_DIR:data, BLYNX_PORT:port,
      BLYNX_LOCAL_ORIGIN:origin, BLYNX_FRONTEND:path.resolve("frontend/dist")}, stdio:"inherit"
  })
  let browser
  try {
    for (let i=0;i<150;i++) {
      try { if ((await fetch(origin+"/api/health")).ok) break } catch {}
      if (app.exitCode!==null || i===149) throw Error("Backend startup failed")
      await new Promise(resolve=>setTimeout(resolve,500))
    }
    browser = await chromium.launchPersistentContext(path.join(data,"browser"), {
      channel:"chromium",headless:true,viewport:{width:1360,height:900},
      args:["--no-sandbox","--disable-extensions-except="+extension,"--load-extension="+extension]
    })
    const page = await browser.newPage()
    await page.goto(origin)
    await page.getByText("Service connected").waitFor()
    await page.getByRole("heading",{name:"Overview"}).waitFor()
    await page.screenshot({path:process.env.BLYNX_SCREENSHOT || "/tmp/blynx-dashboard.png"})
    await page.getByRole("button",{name:"Add streamer",exact:true}).first().click()
    await page.getByLabel("Twitch or YouTube URL").fill("https://example.invalid/watch")
    await page.getByRole("button",{name:"Save streamer"}).click()
    await page.getByRole("alert").getByText("Use a supported Twitch or YouTube channel or video link").waitFor()
    await page.getByRole("button",{name:"Close dialog"}).click()
    await page.getByRole("button",{name:"Chrome Extension",exact:true}).click()
    await page.getByRole("button",{name:"Generate pairing code"}).click()
    const code=await page.getByRole("textbox",{name:"Pairing code"}).inputValue()
    assert(code.length===11)
    const paired=await fetch(origin+"/api/extension/pair",{
      method:"POST",headers:{origin:"chrome-extension://blynx-test","content-type":"application/json"},body:JSON.stringify({code})
    })
    assert.equal(paired.status,200)
    const extHeaders={origin:"chrome-extension://blynx-test",authorization:"Bearer "+(await paired.json()).token}
    const status=await fetch(origin+"/api/extension/status",{headers:extHeaders})
    assert.equal(status.status,200)
    assert.equal((await status.json()).active,0)
    assert.equal((await fetch(origin+"/api/state")).status,401)
    const worker = browser.serviceWorkers()[0] || await browser.waitForEvent("serviceworker")
    const extensionId = new URL(worker.url()).hostname
    const popup = await browser.newPage()
    await popup.goto("chrome-extension://"+extensionId+"/popup.html")
    await page.getByRole("button",{name:"Generate pairing code"}).click()
    await page.waitForFunction(previous=>document.querySelector('input[aria-label="Pairing code"]')?.value!==previous,code)
    await popup.getByLabel("Pairing code").fill(await page.getByRole("textbox",{name:"Pairing code"}).inputValue())
    await popup.getByRole("button",{name:"Pair extension"}).click()
    await popup.getByText("Extension paired",{exact:true}).waitFor()
    await popup.reload()
    await popup.getByText(/Connected ·/).waitFor()
    await page.getByRole("button",{name:"Unpair"}).click()
    await popup.getByRole("button",{name:"Pair extension"}).waitFor()
    await popup.close()
    await page.getByRole("button",{name:"Dashboard",exact:true}).click()
    await page.getByRole("button",{name:"Settings",exact:true}).click()
    await page.getByLabel("Twitch Client ID").waitFor()
    await page.getByLabel("YouTube API key", {exact:true}).waitFor()
    await page.screenshot({path:process.env.BLYNX_SETTINGS_SCREENSHOT || "/tmp/blynx-settings.png"})
    await page.getByRole("button",{name:"Dashboard",exact:true}).click()
    await page.getByRole("button",{name:"Run simulation"}).click()
    await page.getByText("Simulation running",{exact:true}).waitFor()
    const manual=page.getByRole("button",{name:"Save recent footage"})
    await manual.waitFor()
    await page.waitForFunction(()=>Array.from(document.querySelectorAll("button")).some(button=>button.textContent==="Save recent footage"&&!button.disabled))
    await manual.click()
    await page.getByRole("button",{name:"Highlights",exact:true}).click()
    await page.getByRole("button",{name:/Simulation_.*_manual_/}).getByText(/New/).waitFor({timeout:25000})
    await page.getByRole("button",{name:/Simulation_.*_spike-/}).first().getByText(/New/).waitFor({timeout:85000})
    await page.getByRole("button",{name:/Simulation_.*_spike-/}).first().click()
    await page.getByRole("link",{name:"Download MP4"}).waitFor()
    const media=await page.locator("video").getAttribute("src")
    assert(media)
    const ranged=await page.request.get(origin+media,{headers:{range:"bytes=0-511"}})
    assert.equal(ranged.status(),206)
    assert.equal((await ranged.body()).length,512)
    console.log("PASS: browser UI, live WebSocket, invalid URL, real MV3 pairing/reload/revocation, buffered manual clip, automatic clip and video range playback")
  } finally {
    if (browser) await browser.close()
    if (app.exitCode===null) {
      app.kill("SIGINT")
      await new Promise(resolve=>{
        const timer=setTimeout(()=>{app.kill("SIGKILL");resolve()},14000)
        app.once("exit",()=>{clearTimeout(timer);resolve()})
      })
    }
    await rm(data,{recursive:true,force:true})
  }
}
main().catch(error=>{console.error(error);process.exitCode=1})

