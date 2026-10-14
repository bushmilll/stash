# Stash

A free, open-source home for your Instagram saves, built for people who save references: cinematography,
photography, graphic and interior design. Think ShotDeck or FrameSet, but made from your own saves and
living on your own Mac.

- **Frames** view (justified rows, letterboxes trimmed) and a numbered **Index** view
- **Filters** computed from the pixels: color, tone, saturation (incl. black & white), warm/cool, contrast, true aspect ratio
- **AI search** (optional, on-device): type what you're looking for ("flower", "red neon at night") and Stash
  finds it inside every image and video, opening videos at the moment it appears
- **Content tags** (AI): film frames vs. talking to camera, behind the scenes, gear, text
- **Similar shots**, **search by image** (drop any picture onto the window), the color **palette** of every frame
- **Collections**, tags, notes, favorites; export any collection or selection as a zip with a spreadsheet of sources
- **Moodboards**: Sheet (numbered catalog page), Columns or Rows; export PNG or PDF
- **Video tools**: full-res frame grabs, one still per shot, MP4 and GIF clips with in/out points
- **Sync** with Instagram (your saved collections) plus import of Instagram's data export for the backlog
- Trash with undo, daily backups, light and dark

## Install

1. Go to **[Releases](../../releases/latest)** and download:
   - `Stash-apple-silicon.dmg` for M1 / M2 / M3 / M4 Macs
   - `Stash-intel.dmg` for older Intel Macs (Apple menu › About This Mac tells you which)
2. Open the .dmg and drag **Stash** into **Applications**.
3. The first time only: open Stash, and when macOS says it can't verify the developer, click **Done**,
   then go to **System Settings › Privacy & Security**, scroll down, and click **Open Anyway** next to Stash.
   (Stash isn't signed with a paid Apple developer certificate. After this once, it opens normally.)

To update, download the newest release and replace the app in Applications. Your library isn't touched.

## Getting your saves in

Open **Sync** (top right):

1. **Import your backlog:** Instagram › Settings › Accounts Center › Your information and permissions ›
   Download your information › Some of your information › tick **Saved** › format **JSON**. Drop the .zip into Stash.
2. **Connect sync:** be logged into instagram.com in Chrome (or pick your browser), paste the address of each saved
   collection, add your username, Save, then **Sync now**. The first time, your Mac asks if Stash may use
   "Chrome Safe Storage": choose **Always Allow** (that's how Stash reads your Instagram login).
   Safari needs Full Disk Access for Stash in System Settings › Privacy & Security.

Sync is slow on purpose (6–12 seconds between requests) and stops by itself if Instagram pushes back.
Instagram has no official API for saves, so there's always some risk of Instagram asking you to verify your account.

## Where things live

Your library stays on your Mac in your home folder under **Stash**: the database, previews and video stills,
daily backups in `backups`, exports in `exports`, and a log in `stash.log`. Files you download go to Downloads.

The **originals** (the big files) can live elsewhere: Sync › **Storage** › Move to **Dropbox** (or Google Drive,
iCloud Drive, an external drive). Then in Finder right-click the *Stash Media* folder › **Make online-only** to free
the space; Stash fetches an original from the cloud when you open it. Browsing and search keep working even when the
folder is offline.

---

## For developers

Plain Python + vanilla JS, no build step.

```
pip install -r requirements.txt
python -m app            # serves http://localhost:8765 in your browser
python stash_app.py      # desktop window (macOS, needs pywebview)
STASH_SELFTEST=1 python stash_app.py   # end-to-end checks, used by the build
```

- `app/main.py` routes · `app/db.py` SQLite · `app/sync.py` Instagram (gallery-dl) · `app/importer.py` data export
- `app/media.py` ffmpeg tools · `app/analyze.py` palette / look / similarity · `app/static/` the whole UI
- Every push to `main` builds both Mac apps on GitHub Actions (`.github/workflows/build-mac.yml`),
  tests the packaged app, and publishes a release.
- `docker compose up -d` runs it as a server instead (see `docker-compose.yml`).

Fonts: Inter Tight and IBM Plex Mono (SIL Open Font License, see `app/static/fonts`).
