# Theme-to-MP4

A local Streamlit app that extracts transcript timestamps for a theme, downloads only the selected YouTube sections, normalizes the clips, and stitches them into `final_compilation.mp4`.

## What it does

1. Fetches captions and timestamp metadata using `youtube-transcript-api`; it does not download the full video for transcript extraction.
2. Sends timestamped transcript excerpts to either the OpenAI API or a local Ollama model and asks for a JSON array of matching `{ "start": number, "end": number }` intervals.
3. Validates each interval and uses `yt-dlp --download-sections` to download just the selected time ranges.
4. Uses `ffmpeg` to normalize every clip to 1920×1080, 30 fps, H.264 video, and AAC stereo audio, then concatenates the normalized clips.
5. Writes a `manifest.json` with per-video statuses, errors, selected timestamps, and generated file paths.

## Prerequisites

- Python 3.10+ (Python 3.11 recommended)
- `ffmpeg` **and** `ffprobe` installed and available on `PATH`
- An internet connection for YouTube transcript/video requests
- A JavaScript runtime supported by `yt-dlp` (Deno recommended; Node.js is another option) for current YouTube extraction support
- For OpenAI: an OpenAI API key
- For local processing: Ollama installed, running, and a model downloaded

`yt-dlp` (including its default Python extras) is installed by `pip` from `requirements.txt`. A JavaScript runtime such as Deno or Node.js may also be required by current YouTube extraction. The app checks the executable path for `yt-dlp`, `ffmpeg`, and `ffprobe` before starting a run.

### Install FFmpeg

- **macOS (Homebrew):** `brew install ffmpeg`
- **Ubuntu/Debian:** `sudo apt update && sudo apt install -y ffmpeg`
- **Windows:** install an FFmpeg build, add its `bin` directory to `PATH`, and reopen the terminal. Confirm both `ffmpeg -version` and `ffprobe -version` work.

### Install Ollama (optional)

Install Ollama from <https://ollama.com/>, start it, and download a model. For the default UI model:

```bash
ollama pull llama3.2:3b
ollama list
```

The default endpoint is `http://localhost:11434`. You can change it in the UI. The selected model must already be present in Ollama.

## Install and run

### macOS / Linux

```bash
cd theme_to_mp4
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# Optional, for the OpenAI provider:
export OPENAI_API_KEY="your-api-key"

# Check required executables:
ffmpeg -version
ffprobe -version
yt-dlp --version

streamlit run app.py
```

### Windows PowerShell

```powershell
cd theme_to_mp4
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# Optional, for the OpenAI provider:
$env:OPENAI_API_KEY = "your-api-key"

# Check required executables:
ffmpeg -version
ffprobe -version
yt-dlp --version

streamlit run app.py
```

Streamlit prints a local URL (normally `http://localhost:8501`). Choose a provider, enter the theme and URLs, set the preferred transcript languages, then click **Extract and build MP4**.

## Deploy online (Render + Docker)

You can host the app at a public HTTPS URL instead of running it on your computer. This project includes a `Dockerfile` and `render.yaml` configured for Render. A Render account and a Git repository are required; this project cannot be published to a public host directly from this chat.

1. Create a GitHub repository and upload the files from this project ZIP to the repository root (`app.py`, `pipeline.py`, `llm_clients.py`, `requirements.txt`, `Dockerfile`, `render.yaml`, and the `tests/` folder).
2. Sign in to [Render](https://render.com/), choose **New → Blueprint**, and connect the GitHub repository containing `render.yaml`. Alternatively, create a **Web Service** from the repository and select its Docker runtime.
3. Deploy the service. Render will build the Docker image with Python, FFmpeg/ffprobe, and Deno. The app should bind to the port Render provides.
4. In the service's **Environment** settings, add `OPENAI_API_KEY` as a secret if you plan to use OpenAI. Do not put secrets in source files or commit them to Git.
5. Open the `onrender.com` URL Render provides, choose the provider, enter your theme and URLs, and run the pipeline.

### Hosting considerations

- Render's free web services use an ephemeral filesystem and can spin down after inactivity. Generated MP4s and manifests are temporary; download outputs promptly. Free-tier resource and runtime constraints can make long or high-resolution jobs fail. Consult [Render's current free-instance limits](https://render.com/docs/free) before processing large jobs.
- On a hosted server, `http://localhost:11434` points to the server/container, not Ollama on your laptop. For hosted Ollama, provide a secure, reachable endpoint and protect it with appropriate network controls. Do not expose an unauthenticated Ollama port to the public internet.
- OpenAI use incurs API charges according to your account and model. The API key should be configured as a hosting secret.
- Before exposing the app publicly, consider access controls and rate limits; anyone who can reach an unprotected instance may consume compute or API budget.

### Build and run the Docker image locally

```bash
docker build -t theme-to-mp4 .
docker run --rm -p 8501:8501 -e OPENAI_API_KEY="$OPENAI_API_KEY" theme-to-mp4
```

Then open <http://localhost:8501>.

## Output files

Each run creates a unique folder beneath `outputs/`:

```text
outputs/
  run_YYYYMMDD_HHMMSS_<id>/
    raw/                     # Section downloads
    normalized/              # Normalized clip MP4s
    concat_list.txt          # ffmpeg concat list
    final_compilation.mp4    # Final output, if any clips were created
    manifest.json            # Per-video results and timestamps
```

The UI includes a download control for the finished MP4 and the JSON manifest. The intermediate clips stay on disk for inspection.

## Troubleshooting

- **No transcript:** private, age-restricted, unavailable, or caption-disabled videos may not yield transcripts. That URL is marked failed and other URLs continue.
- **No matching segments:** the video is marked `no_matches`; no download is attempted for it.
- **Ollama connection error:** start Ollama and verify the base URL. Run `ollama list` to confirm the selected model is installed.
- **`ffmpeg` or `yt-dlp` missing:** verify each command works in the same terminal/environment used to launch Streamlit. Reopen the terminal after changing `PATH`.
- **YouTube throttling / format errors:** YouTube access can change and may rate-limit requests. Review the per-video error in the UI; update `yt-dlp` with `python -m pip install --upgrade yt-dlp` if needed.
- **Timestamp quality:** LLM-selected intervals are suggestions grounded in transcript timestamps. Review the manifest and output clips before publishing.

## Tests

The tests exercise URL parsing, timestamp validation, and segment merging without making network requests:

```bash
python -m unittest discover -s tests -v
```

## Responsible use

Only download and transform content you own or have permission to use, and comply with applicable laws, licenses, and platform terms. Local Ollama is provided for local processing and privacy/control over the model endpoint; it is not a guarantee of accuracy or a way to override organizational policy. Transcript extraction and video downloading are separate requests, so either may fail independently.

## References

- `youtube-transcript-api`: <https://github.com/jdepoix/youtube-transcript-api>
- `yt-dlp`: <https://github.com/yt-dlp/yt-dlp>
- Ollama API: <https://github.com/ollama/ollama/blob/main/docs/api.md>
- OpenAI structured outputs guide: <https://developers.openai.com/api/docs/guides/structured-outputs>
