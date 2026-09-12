Place soundboard cue folders here.

Each cue now lives under `cues/<folder>/` with its own `cue.toml`.

Typical layout:

- `cues/metal_pipe_drop/cue.toml`
- `cues/metal_pipe_drop/audio.wav`
- `cues/metal_pipe_drop/media.mp4`

Every `cue.toml` can also set its own `volume = 1.0` multiplier. This per-cue value is applied on top of the global soundboard audio volume, so you can make one reaction quieter or louder without retuning the rest.

At startup, the soundboard automatically scans `data/soundboard/cues/*/cue.toml` and loads every cue it finds. Asset paths inside `cue.toml` are relative to that cue folder.

If your cue media is a video with embedded audio, you can import just the video file. The green-screen WebUI will play that video once per trigger with its own audio, without requiring a separate wav file.

If your cue uses a GIF plus a separate audio file, the GIF stays visible until the browser audio finishes instead of stopping on a fixed short timer.

Easy Windows launcher:

- Double-click `plugins\maibot_bilibili_live_adapter_copy\import_soundboard_cue.bat` for a guided import flow.
- You can drag one video file onto the BAT and import it directly as media with embedded audio.
- You can also drag two files to prefill media + separate audio, or audio + media.
- Run `import_soundboard_cue.bat --help` for the full command-line interface.

Example import command:

```powershell
plugins\maibot_bilibili_live_adapter_copy\import_soundboard_cue.bat `
  --cue-id laugh_rip `
  --label "Laugh RIP" `
  --audio "F:\assets\laugh_rip.wav" `
  --media "F:\assets\laugh_rip.gif" `
  --usage-hint "Use when chat is laughing at a whiff, a self-own, or a dramatic fail." `
  --volume 0.7 `
  --keyword haha `
  --keyword lol
```

The BAT wraps `tools/import_soundboard_cue.py`, copies the media into this directory, writes `cue.toml` into that cue folder, and now also lets you set a per-cue volume multiplier during the guided import flow.
