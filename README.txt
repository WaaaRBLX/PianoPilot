PianoPilot 0.8 - plays songs on Roblox pianos for you

START
  Double-click PianoPilot.exe. The app window opens, with no command window.
  Starting it again while it's open just brings the window back. Closing the window closes the app.
  To open it from the desktop or Start menu: Settings (gear, bottom left), "Add to desktop and Start menu".

  The first time, Windows may say "Windows protected your PC". Click More info, then Run anyway.
  Windows shows this for apps that aren't digitally signed by a known publisher. PianoPilot.exe is a tiny starter:
  it only runs pianopilot.py with your Python. Its source code is in the launcher folder.

  "Start PianoPilot.bat" opens the same app with a small command window that shows what's going on.
  Use it if PianoPilot.exe doesn't start, or if your antivirus removes PianoPilot.exe.

  The first time it opens, a short welcome walks you through it. Help (bottom left, or press ?) has a
  quick start, your hotkeys and fixes for common problems.

PLAY A SONG
  1. In Roblox, sit at a piano. Close the chat box.
  2. In PianoPilot, click a song, then press Play (or Space).
     Not sure about the settings? Press "Best for this song" under the title first.
  3. Click into the Roblox window. It counts down 3 seconds and plays.
  Clicking out of Roblox pauses it automatically, so it never types into other windows.

HOTKEYS (work while Roblox is in front; change them in Settings. Inside PianoPilot, Home, End and
Page Up/Down keep their usual jobs, so those four only work in Roblox)
  F6 play / pause    F7 stop    Home back to the start
  Page Down slower   Page Up faster    End next song in the queue

PLAYING SEVERAL SONGS
  - Queue: point at a song and press +, or use "Add to queue" / "Play next". Queued songs play one
    after another. The Queue tab can shuffle, reorder or clear it, or shuffle every song in.
  - Favorites: press the star. Sort the list by name, recently played, length or difficulty.
  - Mini player: "Open the mini player" opens a small window that stays on top of Roblox.

ADD SONGS
  Press "Add a song" (top left) and drop your file into the box, or click "Choose a file".
  Put in any of these and PianoPilot works out the rest:
  - MP3s and videos of piano music: turned into songs. Piano-only recordings come out best.
    The first time, PianoPilot sets this up (one click). It needs PianoModel.pianoscribe in your
    Downloads folder and downloads about 150 MB of Python libraries.
  - MIDI files: added as they are.
  - Text piano sheets (like "[tu] y o | p") as .txt files: opens the sheet window to check the tempo.
  You can also drop files (or a whole folder) anywhere on the window, or put MIDI files in the Songs folder.
  The Add a song window also has "Paste a piano sheet" and "Import a whole folder".
  Any song can be copied or saved as a text sheet: open "Song details and text sheet" on the song page.
  New songs are set up for you as they arrive, including ones you put in the Songs folder while
  PianoPilot was closed. Renaming or moving a song inside the Songs folder keeps its settings.

DELETE A SONG
  Open the song and press Delete (right side, under the title). It asks first, then moves the file to the
  Recycle Bin, so you can still get it back from there. Its settings, star and queue spots are removed.

SONG SETTINGS (saved for each song)
  Best for this song (under the title): one click picks the settings that suit the song (how it fits on
  the piano, pedal, tracks, chord size, cleanup, note length) and says what it changed. Undo puts it back.
  Speed and Play like are yours: it never touches them. New songs get this automatically (Settings).
  The main controls:
    Speed. Play like: Perfect, Pro, Casual or Beginner, or your own mix. Pedal: off, from the song, auto,
    or always on. Loop: the whole song, or a section (move the playhead, press Set A, move again, Set B).
  More controls (the dropdown below them; a tag says how many of these are changed):
    Transpose (with Best fit), Tracks, Feel and Mistakes (the sliders behind Play like), Lock this take,
    Notes outside the piano (move an octave, skip them, or 88 keys), Keys at once, Note length, Cleanup.
  Every control has a small ? that explains it. The song map at the top shows every note; click or drag
  on it to jump. The live keyboard shows the notes falling and the keys being pressed.

SETTINGS (gear, bottom left; apply to every song)
  The calibration test, how your piano's pedal works, hotkeys, "Set up new songs for me", and shortcuts.
  Advanced (a dropdown): the pause around Shift for black keys, chord tightness, key hold time, and a
  custom key layout for unusual pianos. The calibration test sets most of these for you.
  Desktop and Start menu shortcuts. Start over: reset these settings, or every song's settings
  (your songs, favorites and queue always stay).
  Calibration test: Settings, "Run the calibration test". It plays six short tests, asks whether each
  sounded right, and fixes the settings for you.

IF SOMETHING GOES WRONG
  - Something sounds off: run the calibration test (Settings) first, then try Best for this song.
  - Black keys sound like white keys: Settings, Advanced, raise "Black keys" to about 20 ms.
  - Camera flips on black keys: turn off "Shift Lock Switch" in Roblox settings.
  - Nothing is pressed while Roblox runs as administrator: right-click PianoPilot.exe, Run as administrator.
  - PianoPilot closes right after starting: open data\pianopilot.log to see why (Help, "Open the log"),
    or start it with "Start PianoPilot.bat" to watch it in a command window.
  - The command-window version still works: python piano_player.py song.mid (add --help for options).
    It also plays text sheets: python piano_player.py sheet.txt --bpm 100

A NOTE FROM WAAA
  PianoPilot started as a random idea and turned into a passion project. The goal was to test what AI
  can do: nearly everything here, the code, the design and the testing, was made by an AI (Claude, by
  Anthropic), while I steered it and tried each version on real Roblox pianos. It's free and open source,
  and there's no harm meant by it. It just plays music. If anyone asks you to pay for it, it didn't come
  from me. Have fun! - Waaa

CREDITS
  Audio import uses the transcription engine from Pianoscribe by Justagwas (GPL-3.0) and the
  TransKun piano model by Yujia Yan (MIT). Fonts: Instrument Sans and Instrument Serif (SIL OFL).
  See the license files in engine/pianoscribe_engine and ui/fonts.
