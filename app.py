from __future__ import annotations

import os
import re
from pathlib import Path

import streamlit as st

from pipeline import check_system_dependencies, parse_url_list, run_pipeline

st.set_page_config(page_title="Theme-to-MP4", page_icon="🎬", layout="wide")

st.title("🎬 Theme-to-MP4")
st.write("Find theme-relevant moments in YouTube transcripts, download only those sections, normalize the clips, and stitch them into one MP4.")
st.caption("Use only videos you own or have permission to download and edit. YouTube availability, captions, and local rights restrictions may limit a run.")

with st.sidebar:
    st.subheader("System checks")
    dependency_paths = check_system_dependencies()
    for name, path in dependency_paths.items():
        if path:
            st.success(f"{name}: {path}")
        else:
            st.error(f"{name}: not found in PATH")
    st.markdown("---")
    st.markdown("**Pipeline**\n\n1. Fetch transcript and timestamps\n2. Find theme-relevant time ranges\n3. Download selected sections\n4. Normalize clips and concatenate")

provider = "Local Ollama" if st.toggle("Use local Ollama instead of OpenAI API", value=False, help="When enabled, transcript excerpts are sent to the Ollama endpoint you configure, rather than the OpenAI API.") else "OpenAI API"
st.caption(f"Selected provider: **{provider}**")

with st.form("theme_to_mp4_form"):
    theme = st.text_input("Theme", placeholder="e.g., renewable energy policy", help="Describe the subject or specific angle to extract.")
    urls_text = st.text_area(
        "YouTube URLs",
        placeholder="https://www.youtube.com/watch?v=VIDEO_ID\nhttps://youtu.be/VIDEO_ID",
        height=140,
        help="Paste one URL per line, or separate URLs with commas or semicolons. Standard watch, youtu.be, Shorts, live, and embed URLs are supported.",
    )
    col_a, col_b = st.columns(2)
    with col_a:
        if provider == "OpenAI API":
            model = st.text_input("OpenAI model", value="gpt-4o-mini")
            api_key = st.text_input(
                "OpenAI API key",
                type="password",
                value=os.getenv("OPENAI_API_KEY", ""),
                help="Used only for OpenAI API requests. You can set OPENAI_API_KEY in your environment instead.",
            )
            ollama_base_url = "http://localhost:11434"
        else:
            model = st.text_input("Ollama model", value="llama3.2:3b", help="Pull this model first with `ollama pull llama3.2:3b`, or enter another installed model tag.")
            api_key = None
            ollama_base_url = st.text_input(
                "Ollama base URL",
                value=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
                help="When hosted online, localhost refers to the server, not your personal computer. Use a secured, reachable Ollama endpoint if the model runs elsewhere.",
            )
    with col_b:
        language_text = st.text_input("Preferred transcript languages", value="en", help="Comma-separated language codes in preference order, e.g. en,fr,de.")
        st.info("Long transcripts are split into caption-aligned chunks. Each model result is validated before it can trigger a download.")

    submitted = st.form_submit_button("Extract and build MP4", type="primary", use_container_width=True)

if submitted:
    urls = parse_url_list(urls_text)
    languages = [part.strip() for part in language_text.split(",") if part.strip()] or ["en"]
    if not theme.strip():
        st.error("Enter a theme.")
    elif not urls:
        st.error("Paste at least one YouTube URL.")
    elif len(urls) > 20:
        st.error("For reliability, process at most 20 videos per run.")
    elif provider == "OpenAI API" and not (api_key or os.getenv("OPENAI_API_KEY")):
        st.error("Enter an OpenAI API key or set OPENAI_API_KEY before running.")
    elif any(path is None for path in dependency_paths.values()):
        st.error("Install ffmpeg, ffprobe, and yt-dlp and ensure they are available on PATH. Then restart Streamlit.")
    else:
        log_box = st.empty()
        progress = st.progress(0)
        log_lines: list[str] = []

        def log_message(message: str) -> None:
            log_lines.append(message)
            # Update progress when each video begins and keep the UI log bounded.
            match = re.match(r"^\[(\d+)/(\d+)\] Processing", message.strip())
            if match:
                progress.progress(min(99, int((int(match.group(1)) - 1) * 100 / max(1, int(match.group(2))))))
            log_box.code("\n".join(log_lines[-120:]), language="text")

        with st.spinner("Processing videos sequentially…"):
            try:
                result = run_pipeline(
                    theme=theme,
                    urls=urls,
                    provider=provider,
                    model=model.strip(),
                    languages=languages,
                    api_key=api_key,
                    ollama_base_url=ollama_base_url,
                    output_root=Path("outputs"),
                    log=log_message,
                )
                progress.progress(100)
            except Exception as exc:
                st.exception(exc)
                st.stop()

        st.subheader("Run results")
        counts = {
            "Videos submitted": len(urls),
            "Videos with clips": sum(1 for r in result["results"] if r["clips_created"] > 0),
            "Segments found": sum(r["segments_found"] for r in result["results"]),
            "Clips created": len(result["clips"]),
        }
        columns = st.columns(len(counts))
        for column, (label, value) in zip(columns, counts.items()):
            column.metric(label, value)

        table_rows = []
        for video in result["results"]:
            table_rows.append({
                "Video ID": video["video_id"] or "—",
                "Status": video["status"],
                "Transcript language": video["transcript_language"] or "—",
                "Segments found": video["segments_found"],
                "Clips created": video["clips_created"],
                "Errors": " | ".join(video["errors"]),
                "URL": video["url"],
            })
        st.dataframe(table_rows, use_container_width=True, hide_index=True)

        if result["final_path"]:
            final_path = Path(result["final_path"])
            st.success("Compilation created successfully.")
            st.video(str(final_path))
            with final_path.open("rb") as media_file:
                st.download_button(
                    "Download final_compilation.mp4",
                    data=media_file,
                    file_name="final_compilation.mp4",
                    mime="video/mp4",
                    type="primary",
                    use_container_width=True,
                )
        else:
            st.warning("No usable theme-matching clips were created. Review the log and per-video errors above.")
        if result.get("compilation_error"):
            st.error(f"The clips were normalized, but stitching failed: {result['compilation_error']}")

        manifest_path = Path(result["manifest_path"])
        st.download_button(
            "Download run manifest (JSON)",
            data=manifest_path.read_bytes(),
            file_name="manifest.json",
            mime="application/json",
        )
        st.caption(f"Run artifacts are also saved locally in: {Path(result['run_dir']).resolve()}")

st.markdown("---")
st.caption("Note: Local Ollama sends transcript text to the local model endpoint you configured. OpenAI processing sends transcript excerpts to the OpenAI API. Check your organization's policies before processing sensitive or confidential transcripts.")
