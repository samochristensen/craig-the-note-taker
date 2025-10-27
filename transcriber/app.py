import os, glob, json, subprocess, tempfile
from fastapi import FastAPI
from pydantic import BaseModel

DEVICE = os.environ.get("WHISPERX_DEVICE","auto")
MODEL  = os.environ.get("WHISPERX_MODEL","large-v2")

app = FastAPI()

@app.get("/")
def index():
    return {"ok": True, "service": "transcriber", "endpoints": ["/transcribe", "/health"]}

@app.get("/health")
def health():
    return {"ok": True}

class Job(BaseModel):
    session_id: str

@app.post("/transcribe")
def transcribe(job: Job):
    sid = job.session_id
    session_dir = f"/app/data/sessions/{sid}"
    wavs = sorted(glob.glob(f"{session_dir}/user_*.wav"))
    if not wavs:
        return {"error":"no audio"}

    # Merge tracks for a single transcript but keep per-speaker files for diarization help
    merged = f"{session_dir}/merged.wav"
    merge_tracks(wavs, merged)

    # Run WhisperX
    # 1) ASR
    txt = f"{session_dir}/asr.json"
    run(["python","-m","whisperx","transcribe", merged,
         "--model", MODEL, "--output_format", "json",
         "--output_dir", session_dir,
         "--device", device_arg()])
    # 2) Align (optional but improves timings)
    run(["python","-m","whisperx","align",
         f"{session_dir}/{os.path.basename(merged)}.json",
         merged,"--output_dir", session_dir,
         "--device", device_arg()])
    # 3) Diarize (optional if CUDA): you can add pyannote pipeline if desired

    # Export SRT
    run(["python","-m","whisperx","to_srt",
         f"{session_dir}/{os.path.basename(merged)}.json",
         "--output", f"{session_dir}/transcript.srt"])

    # Build plain text
    transcript_text = extract_plain_text(f"{session_dir}/{os.path.basename(merged)}.json")
    with open(f"{session_dir}/transcript.txt","w") as f:
        f.write(transcript_text)

    return {"ok": True, "session_id": sid, "transcript_text": transcript_text[:200000]}  # cap to sane size for prompt

def device_arg():
    return "cuda" if DEVICE in ("auto","cuda") and torch_available_cuda() else "cpu"

def torch_available_cuda():
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False

def run(cmd):
    print("+", " ".join(cmd))
    subprocess.check_call(cmd)

def merge_tracks(paths, out_path):
    # loudest wins mix: use ffmpeg amerge or amix (simple)
    # amix normalizes volume across tracks
    inputs = []
    for p in paths:
        inputs += ["-i", p]
    cmd = ["ffmpeg", *inputs, "-filter_complex", f"amix=inputs={len(paths)}:normalize=1", "-ac", "2", "-ar", "48000", "-y", out_path]
    run(cmd)

def extract_plain_text(json_path):
    with open(json_path,"r") as f:
        data = json.load(f)
    # Handle whisperx json schema
    segments = data.get("segments", [])
    lines = []
    for seg in segments:
        spk = seg.get("speaker", "")
        txt = seg.get("text", "").strip()
        if spk:
            lines.append(f"{spk}: {txt}")
        else:
            lines.append(txt)
    return "\n".join(lines)
