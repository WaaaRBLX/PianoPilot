# Changelog

## 1.0.0 (in testing)

- **No Python install needed.** The download now includes its own Python (the `runtime` folder), so you unzip and double-click. MP3 import installs its libraries into that same copy (it's Python 3.13, which those libraries support), so new users never see the old "Use Python 3.13" step. If you set up MP3 import in 0.8, you may need to click Set up once more.
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
