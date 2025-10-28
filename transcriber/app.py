import os, glob, json, subprocess, tempfile, re
from fastapi import FastAPI, HTTPException
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
    
    # SECURITY: Validate session_id format to prevent path traversal
    # Expected format: YYYYMMDD_HHMMSS (e.g., 20231028_143022)
    if not re.match(r'^\d{8}_\d{6}$', sid):
        raise HTTPException(status_code=400, detail="Invalid session_id format. Expected: YYYYMMDD_HHMMSS")
    
    session_dir = f"/app/data/sessions/{sid}"
    
    # Verify session directory exists
    if not os.path.exists(session_dir):
        raise HTTPException(status_code=404, detail=f"Session directory not found: {sid}")
    
    # Find audio files
    wavs = sorted(glob.glob(f"{session_dir}/user_*.wav"))
    if not wavs:
        raise HTTPException(status_code=400, detail="No audio files found in session directory")

    try:
        # Merge tracks for a single transcript but keep per-speaker files for diarization help
        merged = f"{session_dir}/merged.wav"
        merge_tracks(wavs, merged)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Audio merge failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error during audio merge: {e}")

    # Run WhisperX
    try:
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
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"WhisperX processing failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error during transcription: {e}")

    # Build plain text
    try:
        transcript_text = extract_plain_text(f"{session_dir}/{os.path.basename(merged)}.json")
        with open(f"{session_dir}/transcript.txt","w") as f:
            f.write(transcript_text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to extract transcript text: {e}")

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
    """Execute a subprocess command with logging."""
    print("+", " ".join(cmd))
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError as e:
        print(f"Command failed with exit code {e.returncode}: {' '.join(cmd)}")
        raise
    except FileNotFoundError:
        print(f"Command not found: {cmd[0]}")
        raise subprocess.CalledProcessError(127, cmd)

def merge_tracks(paths, out_path):
    # loudest wins mix: use ffmpeg amerge or amix (simple)
    # amix normalizes volume across tracks
    inputs = []
    for p in paths:
        inputs += ["-i", p]
    cmd = ["ffmpeg", *inputs, "-filter_complex", f"amix=inputs={len(paths)}:normalize=1", "-ac", "2", "-ar", "48000", "-y", out_path]
    run(cmd)

def extract_plain_text(json_path):
    """Extract plain text from WhisperX JSON output with error handling."""
    try:
        with open(json_path,"r") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"Transcript JSON not found: {json_path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in transcript file: {e}")
    
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
