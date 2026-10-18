// Can this Mac play an untouched VP9 file (Instagram's sharpest version, just re-wrapped, not re-encoded)?
import AVFoundation
import WebKit
import AppKit
setbuf(stdout, nil)

let path = CommandLine.arguments[1]
let url = URL(fileURLWithPath: path)
let asset = AVURLAsset(url: url)
let sem = DispatchSemaphore(value: 0)
var playable = false, decoded = 0
Task.detached {
    playable = (try? await asset.load(.isPlayable)) ?? false
    if let track = try? await asset.loadTracks(withMediaType: .video).first, let reader = try? AVAssetReader(asset: asset) {
        let out = AVAssetReaderTrackOutput(track: track, outputSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
        reader.add(out); reader.startReading()
        while let s = out.copyNextSampleBuffer() { if CMSampleBufferGetImageBuffer(s) != nil { decoded += 1 } }
    }
    sem.signal()
}
sem.wait()
print("::notice title=\(url.lastPathComponent) macOS::playable=\(playable) frames_decoded=\(decoded)")

// the same file in a WKWebView, which is what Stash's window is
class D: NSObject, WKNavigationDelegate, WKScriptMessageHandler {
    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        print("::notice title=\(url.lastPathComponent) Stash window::\(m.body)"); exit(0)
    }
}
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let d = D()
let cfg = WKWebViewConfiguration()
cfg.mediaTypesRequiringUserActionForPlayback = []
cfg.userContentController.add(d, name: "r")
let web = WKWebView(frame: NSRect(x: 0, y: 0, width: 400, height: 400), configuration: cfg)
let dir = url.deletingLastPathComponent()
let html = """
<video id=v muted playsinline src="\(url.lastPathComponent)"></video><script>
const v=document.getElementById('v'); const r=(s)=>webkit.messageHandlers.r.postMessage(s);
let frames=0;
v.addEventListener('error',()=>r('ERROR code '+(v.error&&v.error.code)));
v.addEventListener('loadeddata',()=>{ v.play().then(()=>{ const t0=performance.now();
  const tick=()=>{ frames++; if(performance.now()-t0>1500) r('plays: '+v.videoWidth+'x'+v.videoHeight+' t='+v.currentTime.toFixed(2)+' frames='+frames); else (v.requestVideoFrameCallback?v.requestVideoFrameCallback(tick):requestAnimationFrame(tick)); };
  tick(); }).catch(e=>r('play() refused: '+e)); });
setTimeout(()=>r('TIMEOUT readyState='+v.readyState+' canPlay='+v.canPlayType('video/mp4; codecs="vp09.00.40.08"')),8000);
</script>
"""
let page = dir.appendingPathComponent("t.html")
try! html.write(to: page, atomically: true, encoding: .utf8)
let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 400, height: 400), styleMask: [.titled], backing: .buffered, defer: false)
win.contentView = web
win.orderFrontRegardless()
web.loadFileURL(page, allowingReadAccessTo: dir)
DispatchQueue.main.asyncAfter(deadline: .now() + 20) { print("::notice title=\(url.lastPathComponent) Stash window::no answer from the page in 20s"); exit(0) }
app.run()
