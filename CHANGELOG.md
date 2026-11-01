# Changelog

## 1.0.0 (in testing)

- **No Python install needed.** The download now includes its own Python (the `runtime` folder), so you unzip and double-click. MP3 import installs its libraries into that same copy (it's Python 3.13, which those libraries support), so new users never see the old "Use Python 3.13" step. If you set up MP3 import in 0.8, you may need to click Set up once more.
- **Playlists.** A Playlists tab: make, rename and delete playlists (with Undo), add songs from a pick-songs window, the song page or by right-clicking any song, and reorder them.
- **Play, Shuffle and Smart shuffle** above every list, search results included. Smart shuffle plays favorites and songs you haven't heard in a while more often, songs you skip less often, never the same artist twice in a row, and on a playlist mixes in a few similar songs (marked ✦). If it replaces your queue, you can bring the old one back.
- **Keep playing:** when the queue ends, Smart shuffle carries on (switch in the Queue tab).
- **Right-click menu** on songs: play, play next, queue, add to a playlist, favorite.
- **Play counts**, and two new sorts: Most played and Newest.
- **Faster:** PianoPilot remembers what it read from your songs, so it starts quickly with a big library and only loads a song's notes when it's needed. The song map draws half as often while you're in Roblox, leaving more of the PC to Roblox. MP3s go to the graphics card in bigger batches (it remembers if your card needs smaller ones), and the log says where conversion time goes.
- **Starting songs:** pressing F6 inside Roblox starts right away (no countdown); the countdown for the Play button can be changed in Settings.
- **Update check.** Once a day PianoPilot asks GitHub whether a newer version is out and shows a small note at the top of the song list. Nothing about you or your songs is sent; turn it off in Settings.
- **System requirements** are in the README and in the app's README.txt.
- **Any keyboard layout works.** Roblox reads keys by position, and PianoPilot presses them by position, so AZERTY, QWERTZ and other layouts play the same notes as QWERTY.
- A new tip in Help for fast songs that drop notes (keep Roblox at a steady 60 fps).
- MP3 setup gives clearer messages when a download fails (offline, antivirus, or a folder PianoPilot can't write to).

## 0.8.0 (beta)

The first public release.

- Plays MIDI files and text sheets, and turns MP3s and videos of piano music into songs.
- **Best for this song**: one click picks the settings that suit a song, says what it changed, and has Undo. New songs are set up automatically, including songs added while PianoPilot was closed.
- A simpler song page (Speed, Play like, Pedal, Loop), with the rest under **More controls**; Settings has an **Advanced** section.
- Play like a person (Perfect, Pro, Casual, Beginner), sustain pedal, speed, loops, queue, favorites and a mini player.
- A calibration test, hotkeys (F6 play and pause, F7 stop), a first-time welcome, and **?** hints on every control.
- Delete songs (to the Recycle Bin); renaming or moving a song keeps its settings.
