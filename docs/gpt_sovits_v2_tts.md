# GPT-SoVITS v2 TTS

This plugin can synthesize outbound replies through a local GPT-SoVITS v2 API, play the generated wav to VB-Cable, and let VTube Studio use its native Mic Lip Sync instead of the plugin's own mouth timeline.

## Start GPT-SoVITS v2

```powershell
Set-Location "<GPT-SoVITS-v2 安装目录>"
.\runtime\python.exe .\api_v2.py -a 127.0.0.1 -p 9880 -c .\GPT_SoVITS\configs\tts_infer.yaml
```

## Plugin Config

Fill the `tts` and `live2d.sync` sections in `plugins/maibot_bilibili_live_adapter/config.toml`:

```toml
[tts]
enabled = true
provider = "gpt_sovits_v2"
base_url = "http://127.0.0.1:9880"
text_lang = "zh"
ref_audio_path = "F:/path/to/reference.wav"
prompt_text = "reference transcript"
prompt_lang = "zh"
text_split_method = "cut5"
audio_playback_enabled = true
audio_output_device = "CABLE Input"
audio_output_volume = 1.0

[live2d.sync]
mouth_sync_mode = "vts_native"
viseme_timeline_enabled = false
lip_sync_only_mode = false
```

## Native VTS Lip Sync Flow

1. The plugin sends `POST /tts` to GPT-SoVITS v2.
2. Returned wav files are stored under `plugins/maibot_bilibili_live_adapter/data/tts_output` by default.
3. The plugin plays each wav to the configured Windows output device, usually `CABLE Input (VB-Audio Virtual Cable)`.
4. VTube Studio listens to the matching VB-Cable input in its own Mic Lip Sync settings and drives the mouth natively.
5. The plugin still sends non-mouth Live2D timeline events such as prepare/start/end, body sway, and emotion/body support parameters, but it no longer injects `ParamMouthOpenY` or `ParamMouthForm` while `live2d.sync.mouth_sync_mode = "vts_native"`.
6. The subtitle WebUI can keep rendering text, but it does not own audio playback in this mode.

## VTube Studio Setup

1. Install VB-Cable and reboot if Windows asks.
2. In VTube Studio, open Mic Lip Sync and select the VB-Cable input side that receives the plugin audio.
3. Keep the plugin output device on `CABLE Input` so the wav is routed into that cable.
4. If you also want to hear the voice locally, mirror the VB-Cable device in Windows or your audio mixer instead of turning the WebUI audio back on.

## Notes

- `sounddevice` is used for local wav playback to the VB-Cable output device.
- The plugin still computes amplitude metadata from wav files because it is cheap and can help with debugging, but native VTS mouth sync mode does not use those values to drive mouth parameters.
- External aligners such as Rhubarb, WhisperX, or MFA are not recommended for the live path by default. With VB-Cable routing, VTube Studio already sees the real audio waveform directly, which is a better fit for live playback than a second plugin-side mouth animation system.
